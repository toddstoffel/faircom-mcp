# Client patterns for FairCom MCP

## Retry with backoff

Use retries for transient failures only. Validation and authorization errors should fail fast.

```python
import random
import time


def retry_with_backoff(func, *, max_retries=3, base_backoff_s=1.0):
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as exc:
            retryable = getattr(exc, "retryable", False)
            if not retryable or attempt == max_retries - 1:
                raise
            delay = base_backoff_s * (2**attempt) + random.uniform(0, 0.25)
            time.sleep(delay)
```

## Safe write pattern

1. Read first to confirm the target rows.
2. Preview with `dry_run=True` for write operations.
3. Apply only with `confirm_write=True` and `dry_run=False`.
4. Review audit and metrics output after execution.

## Circuit breaker pattern

For long-running automation, a simple circuit breaker helps avoid hammering an unhealthy FairCom endpoint.

```python
class FaircomCircuitBreaker:
    def __init__(self, *, failure_threshold=5, recovery_timeout_s=60.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self.failure_count = 0
        self.open_until = 0.0

    def call(self, func):
        if self.open_until > time.time():
            raise RuntimeError("circuit breaker open")
        try:
            result = func()
            self.failure_count = 0
            return result
        except Exception:
            self.failure_count += 1
            if self.failure_count >= self.failure_threshold:
                self.open_until = time.time() + self.recovery_timeout_s
            raise
```
