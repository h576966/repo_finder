def decorrelated_jitter_retry_delay(previous_delay, base_delay, random_value):
    upper_bound = max(base_delay, previous_delay * 3)
    return min(upper_bound, base_delay + random_value)
