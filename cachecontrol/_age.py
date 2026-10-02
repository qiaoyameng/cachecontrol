from typing import TypeGuard


AGE_CORRECTED_INITIAL_AGE = "_cachecontrol_corrected_initial_age"
AGE_RECORD_ID = "_cachecontrol_age_record_id"
AGE_RESPONSE_MONOTONIC_TIME = "_cachecontrol_response_monotonic_time"
AGE_RESPONSE_WALL_TIME = "_cachecontrol_response_wall_time"
AGE_REQUEST_MONOTONIC_TIME = "_cachecontrol_request_monotonic_time"
AGE_REQUEST_WALL_TIME = "_cachecontrol_request_wall_time"


def is_real_number(value: object) -> TypeGuard[float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
