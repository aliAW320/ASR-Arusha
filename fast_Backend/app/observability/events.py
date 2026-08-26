from enum import StrEnum


class LogEvent(StrEnum):
    SERVICE_STARTED = "service_started"
    SERVICE_STOPPED = "service_stopped"
    HTTP_REQUEST_COMPLETED = "http_request_completed"
    HTTP_REQUEST_FAILED = "http_request_failed"
    UNHANDLED_EXCEPTION = "unhandled_exception"
    AUTHENTICATION_FAILED = "authentication_failed"
    OBJECT_STORAGE_UPLOAD_FAILED = "object_storage_upload_failed"
    OBJECT_STORAGE_DELETE_FAILED = "object_storage_delete_failed"
    DATABASE_WRITE_RETRY = "database_write_retry"
    DATABASE_WRITE_FAILED = "database_write_failed"
    UPLOAD_CLEANUP_FAILED = "upload_cleanup_failed"
