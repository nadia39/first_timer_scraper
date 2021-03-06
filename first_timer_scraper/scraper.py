import os
import requests
import threading
import time
from .cache import NoCache
from .response import Response
from .repository import Repository
from .concurrency import unique_step
#import hanging_threads
#hanging_threads.start_monitoring()

WAIT_FOR_RETRY_IN_SECONDS = 60
HEADER_MATCH = "If-None-Match"
HEADER_MODIFIED = "If-Modified-Since"

_print = print
PRINT_LOCK = threading.RLock() # use an rlock if something goes wrong with recursion
def print(*args, **kw):
    with PRINT_LOCK:
        return _print(*args, **kw)
        
def secure_auth_print(auth):
    if auth is None:
        return None
    if isinstance(auth, (tuple,list)):
        return auth[0]
    else:
        return "<??>"

def credentials_for_requests(credentials):
    if credentials is None:
        return credentials
    if isinstance(credentials, list):
        return tuple(credentials)
    return credentials

class Scraper:

    def __init__(self, credentials, model):
        self._credentials = credentials
        self._model = model
        self._lock = threading.Lock()
        self._cache = NoCache()
        self._requesting_lock = threading.Lock()
        self._requesting = {} # url : future
        
    @unique_step
    def get(self, url):
        """Return a Response for the URL or None"""
        # Set User-Agent header
        #   https://developer.github.com/v3/#user-agent-required
        headers = {"User-Agent" : "niccokunzmann/first_timer_scraper"}
        ok = False
        while not ok:
            headers.pop(HEADER_MATCH, None)
            headers.pop(HEADER_MODIFIED, None)
            with self._lock:
                cached_result = self._cache.get_response(url)
                if cached_result:
                    print("cached", url)
                    etag = cached_result.headers.get("ETag")
                    if etag:
                        headers[HEADER_MATCH] = etag
                    last_modified = cached_result.headers.get("Last-Modified")
                    if last_modified:
                        headers[HEADER_MODIFIED] = last_modified
            for auth in self._credentials:
                print("GET", url, "as", secure_auth_print(auth))
                response = requests.get(url, headers=headers, auth=credentials_for_requests(auth))
                rate_limit = response.headers.get("X-RateLimit-Remaining")
                if response.status_code == 304:
                    assert cached_result
                    # not modified
                    print("GET", url, "cached and not modified")
                    return cached_result
                elif response.status_code == 200:
                    ok = True
                    break
                elif response.status_code == 403:
                    with self._lock:
                        print("GET", url, "used up", secure_auth_print(auth), "limit", rate_limit)
                        self._credentials.used_up(auth)
                else:
                    print("GET", url, "ERROR:", response.status_code, response.reason)
                    response.raise_for_status()
            if not ok:
                time.sleep(WAIT_FOR_RETRY_IN_SECONDS)
                print("GET", url, "waiting")
        print("GET", url, "calls left:", rate_limit)
        result = Response.from_response(response)
        # first cache, then remove the future
        with self._lock:
            self._cache.cache_response(result)
        return result
    
    def set_cache(self, cache):
        self._cache = cache
        
    def start(self):
        pass
                
    def scrape_organization(self, organization):
        """Add all repositories from the organization."""
        print("scrape organization", organization)
        self._model.update_requested(organization)
        @self.get_each("https://api.github.com/orgs/{}/repos".format(organization))
        def add_repository(repository):
            self.scrape_repository(repository["full_name"])
    
    def get_each(self, url):
        """Request a url"""
        def add_call(function):
            @self.get(url)
            def call_each(result):
                if result.next_page:
                    self.get(result.next_page)(call_each)
                for element in result.json:
                    function(element)
        return add_call
        
    @unique_step
    def clone(self, full_name):
        with self._lock:
            repository = self._cache.get_repository(full_name)
        if repository:
            return repository
        repository = Repository(full_name)
        with self._lock:
            self._cache.cache_repository(repository)
        repository.update()
        return repository
    
    def scrape_repository(self, full_name, update_organization=False):
        print("scrape repository", full_name)
        self._model.update_requested(full_name)
        @self.clone(full_name)
        def when_cloned(repo):
            if repo.can_have_first_timers():
                commits = repo.commits
                @self.get_each(repo.pull_requests_url)
                def retrieved_pull_request(pr):
                    print("got", pr["html_url"])
                    head_commit = pr["head"]["sha"]
                    if repo.is_first_timer_commit(head_commit):
                        print("first timer:", pr["html_url"])
                        github_user = pr["head"]["user"]["login"]
                        self._model.add_first_timer_contribution(
                            github_user, full_name, pr["number"],
                            pr["created_at"])
__all__ = ["Scraper"]
