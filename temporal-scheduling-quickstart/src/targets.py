from dataclasses import dataclass


@dataclass
class Target:
    name: str
    url: str
    expected_status: int
    timeout_seconds: float = 5.0


TARGETS: list[Target] = [
    Target(name="httpbin-ok", url="https://httpbin.org/status/200", expected_status=200),
    Target(name="httpbin-flaky", url="https://httpbin.org/status/500", expected_status=200),
    Target(name="httpbin-slow", url="https://httpbin.org/delay/2", expected_status=200, timeout_seconds=4.0),
]
