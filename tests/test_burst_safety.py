from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException
from starlette.requests import Request

from app.core import deps


def _request(ip="burst-client"):
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/queries",
        "headers": [],
        "query_string": b"",
        "client": (ip, 1234),
        "server": ("testserver", 80),
        "scheme": "http",
    })


def test_sixty_simultaneous_public_requests_are_bounded():
    deps._rate_hits.clear()

    def attempt(_):
        try:
            deps.throttle_public_chat(_request())
            return "allowed"
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=60) as pool:
        outcomes = list(pool.map(attempt, range(60)))

    assert outcomes.count("allowed") == deps._RATE_LIMIT
    assert outcomes.count(429) == 60 - deps._RATE_LIMIT
    deps._rate_hits.clear()
