import logging
import os
import threading
import uuid
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Optional

from app.core.config import settings
from app.integrations.prodocux_facade import ProDocuXFacade
from app.models.activity import ActivityEventType
from app.models.file_record import FileRecord, IngestionStatus
from app.services.activity_service import ActivityService
from app.services.storage import MemoryStore

logger = logging.getLogger("studiotower.ingestion_runner")


def load_file_binary(store: MemoryStore, file_rec: FileRecord) -> Optional[bytes]:
    """
    Robustly loads binary content for a file record across memory cache, GCS, and local disk.
    Returns None on read failure or missing file (fail-closed, never masking missing data with empty bytes).
    Returns b'' ONLY if file is genuinely verified to have size_bytes == 0.
    """
    # 1. Check memory store blob cache
    cached = store.get_file_blob(file_rec.file_id)
    if cached is not None:
        return cached

    # 2. Check storage path
    if not file_rec.storage_path:
        if file_rec.size_bytes == 0:
            return b""
        return None

    try:
        if settings.STUDIO_TOWER_ARTIFACT_BACKEND == "gcs":
            from google.cloud import storage as gcs_storage
            gcs_client = gcs_storage.Client()
            bucket = gcs_client.bucket(settings.STUDIO_TOWER_GCS_BUCKET)
            blob = bucket.blob(file_rec.storage_path)
            if not blob.exists():
                logger.error(f"GCS blob not found for file {file_rec.file_id} at {file_rec.storage_path}")
                return None
            data = blob.download_as_bytes()
            store.save_file_blob(file_rec.file_id, data)
            return data
        else:
            data_dir_abs = os.path.abspath(settings.STUDIO_TOWER_DATA_DIR)
            local_path = os.path.abspath(os.path.join(data_dir_abs, file_rec.storage_path))
            if not os.path.exists(local_path):
                logger.error(f"Local file not found for file {file_rec.file_id} at {local_path}")
                return None
            with open(local_path, "rb") as f:
                data = f.read()
            store.save_file_blob(file_rec.file_id, data)
            return data
    except Exception as e:
        logger.exception(f"Failed to read file binary for file {file_rec.file_id}: {e}")
        return None


class IngestionJobRunner(ABC):
    """Abstract job runner for asynchronous document ingestion and indexing."""

    @abstractmethod
    def submit_ingestion(
        self,
        space_id: str,
        file_id: str,
        content_bytes: bytes,
        filename: str,
        store: MemoryStore,
    ) -> str:
        """Submits an ingestion job and returns the job ID."""
        pass

    @abstractmethod
    def recover_expired_leases(self, store: MemoryStore) -> list[FileRecord]:
        """Scans and reclaims expired ingestion leases."""
        pass


def execute_ingestion_job(
    space_id: str,
    file_id: str,
    content_bytes: bytes,
    filename: str,
    store: MemoryStore,
    worker_id: str,
    target_generation: Optional[int] = None,
    expected_job_id: Optional[str] = None,
) -> bool:
    """
    Executes the ingestion pipeline with generation fencing and atomic active generation commit.
    """
    f = store.get_file(file_id)
    if not f or f.space_id != space_id:
        logger.warning(f"File {file_id} not found or space mismatch for space {space_id}")
        return False

    # Idempotent return: if this exact target generation is already committed and READY/READY_PARTIAL, succeed
    if (
        target_generation is not None
        and f.active_generation == target_generation
        and f.ingestion_status in (IngestionStatus.READY, IngestionStatus.READY_PARTIAL)
    ):
        logger.info(f"Ingestion for {file_id} generation {target_generation} already completed.")
        return True

    if expected_job_id and target_generation:
        # Worker executing pre-claimed job dispatched by Cloud Tasks
        if f.ingestion_job_id != expected_job_id:
            logger.warning(
                f"Worker rejected stale job {expected_job_id} for file {file_id} "
                f"(current active job is {f.ingestion_job_id})"
            )
            return False
        if f.ingestion_lease_until is not None and f.ingestion_lease_until < datetime.now(UTC):
            logger.warning(f"Worker rejected expired lease for job {expected_job_id} on {file_id}")
            return False
        job_id = expected_job_id
        target_gen = target_generation
    else:
        # 1. Acquire fresh ingestion lease
        acquired, file_rec, target_gen = store.acquire_ingestion_lease(
            space_id=space_id,
            file_id=file_id,
            worker_id=worker_id,
            lease_seconds=60,
        )
        if not acquired or not file_rec or not file_rec.ingestion_job_id:
            logger.warning(f"Could not acquire ingestion lease for file {file_id} in space {space_id}")
            return False
        job_id = file_rec.ingestion_job_id

    try:
        # 2. Extract and parse document using hardened multi-format adapters
        status, chunks, extracted_pages, ocr_gaps, ext_method, err_msg = ProDocuXFacade.ingest_document(
            file_content=content_bytes,
            filename=filename,
            file_id=file_id,
            space_id=space_id,
            generation=target_gen,
        )

        # 3. Save chunks into generation-partitioned storage with job ownership verification
        saved = store.save_document_chunks(
            space_id=space_id,
            file_id=file_id,
            generation=target_gen,
            chunks=chunks,
            expected_job_id=job_id,
        )
        if not saved:
            logger.warning(f"Failed to save document chunks for file {file_id} (job {job_id})")
            return False

        # 4. Atomic commit: swap active generation and finalize status
        if status in (IngestionStatus.READY, IngestionStatus.READY_PARTIAL, IngestionStatus.NEEDS_OCR):
            committed = store.commit_ingestion_generation(
                space_id=space_id,
                file_id=file_id,
                target_generation=target_gen,
                expected_job_id=job_id,
                chunk_count=len(chunks),
                pages=extracted_pages,
                ocr_gaps=ocr_gaps,
                status=status,
                error_code=None,
                error_msg=None,
            )
            if not committed:
                logger.warning(f"Failed to CAS commit ingestion generation {target_gen} for file {file_id}")
                return False

            f_rec = store.get_file(file_id)
            if f_rec:
                if status == IngestionStatus.NEEDS_OCR:
                    ev_type = ActivityEventType.FILE_NEEDS_OCR
                elif status == IngestionStatus.READY_PARTIAL:
                    ev_type = ActivityEventType.FILE_INGESTION_PARTIAL
                else:
                    ev_type = ActivityEventType.FILE_INGESTION_READY

                ActivityService.record_file_event(
                    space_id=space_id,
                    event_type=ev_type,
                    file_record=f_rec,
                    extra_details={"pages": extracted_pages, "chunks": len(chunks)},
                )
        else:
            store.fail_ingestion_generation(
                space_id=space_id,
                file_id=file_id,
                expected_job_id=job_id,
                error_code="ERR_INGESTION_PARSER_FAILED",
                error_msg=err_msg or "Document parsing failed.",
            )
            f_rec = store.get_file(file_id)
            if f_rec:
                ActivityService.record_file_event(
                    space_id=space_id,
                    event_type=ActivityEventType.FILE_INGESTION_FAILED,
                    file_record=f_rec,
                    extra_details={"error_msg": err_msg or "Document parsing failed."},
                )
            return False

        logger.info(
            f"Successfully completed ingestion for file {file_id} at generation {target_gen} "
            f"(status={status}, chunks={len(chunks)}, pages={extracted_pages})"
        )
        return True

    except Exception as e:
        logger.exception(f"Unhandled exception during document ingestion for file {file_id}: {e}")
        store.fail_ingestion_generation(
            space_id=space_id,
            file_id=file_id,
            expected_job_id=job_id,
            error_code="ERR_INTERNAL_INGESTION_EXCEPTION",
            error_msg="Internal error during document processing.",
        )
        f_rec = store.get_file(file_id)
        if f_rec:
            ActivityService.record_file_event(
                space_id=space_id,
                event_type=ActivityEventType.FILE_INGESTION_FAILED,
                file_record=f_rec,
                extra_details={"error_msg": str(e)},
            )
        return False


class IngestionDispatchError(Exception):
    """Raised when enqueuing an ingestion task to Cloud Tasks or background queue fails."""
    pass


class InlineIngestionRunner(IngestionJobRunner):
    """Executes document ingestion inline synchronously."""

    def submit_ingestion(
        self,
        space_id: str,
        file_id: str,
        content_bytes: bytes,
        filename: str,
        store: MemoryStore,
    ) -> str:
        worker_id = f"worker_inline_{uuid.uuid4().hex[:8]}"
        execute_ingestion_job(space_id, file_id, content_bytes, filename, store, worker_id)
        f = store.get_file(file_id)
        return f.ingestion_job_id if f and f.ingestion_job_id else f"job_inline_{file_id}"

    def recover_expired_leases(self, store: MemoryStore) -> list[FileRecord]:
        reclaimed = store.scan_and_reclaim_expired_ingestion_leases()
        successful = []
        for f in reclaimed:
            try:
                content = load_file_binary(store, f)
                if content is None:
                    logger.error(f"Cannot recover file {f.file_id}: binary content unavailable on storage")
                    store.fail_ingestion_generation(
                        space_id=f.space_id,
                        file_id=f.file_id,
                        expected_job_id=f.ingestion_job_id,
                        error_code="ERR_STORAGE_READ_FAILED",
                        error_msg="Binary content unavailable on storage during recovery.",
                    )
                    continue
                self.submit_ingestion(f.space_id, f.file_id, content, f.filename, store)
                successful.append(f)
            except Exception as e:
                logger.warning(f"Failed to re-dispatch reclaimed file {f.file_id}: {e}")
        return successful


class ThreadPoolIngestionRunner(IngestionJobRunner):
    """Executes document ingestion asynchronously via bounded thread pool."""

    def __init__(self, max_workers: int = 4):
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ingest_worker")

    def submit_ingestion(
        self,
        space_id: str,
        file_id: str,
        content_bytes: bytes,
        filename: str,
        store: MemoryStore,
    ) -> str:
        worker_id = f"worker_tp_{uuid.uuid4().hex[:8]}"
        self._executor.submit(
            execute_ingestion_job,
            space_id,
            file_id,
            content_bytes,
            filename,
            store,
            worker_id,
        )
        return f"job_tp_dispatched_{file_id}"

    def recover_expired_leases(self, store: MemoryStore) -> list[FileRecord]:
        reclaimed = store.scan_and_reclaim_expired_ingestion_leases()
        successful = []
        for f in reclaimed:
            try:
                content = load_file_binary(store, f)
                if content is None:
                    logger.error(f"Cannot recover file {f.file_id}: binary content unavailable on storage")
                    store.fail_ingestion_generation(
                        space_id=f.space_id,
                        file_id=f.file_id,
                        expected_job_id=f.ingestion_job_id,
                        error_code="ERR_STORAGE_READ_FAILED",
                        error_msg="Binary content unavailable on storage during recovery.",
                    )
                    continue
                self.submit_ingestion(f.space_id, f.file_id, content, f.filename, store)
                successful.append(f)
            except Exception as e:
                logger.warning(f"Failed to re-dispatch reclaimed file {f.file_id}: {e}")
        return successful

    def shutdown(self, wait: bool = False):
        self._executor.shutdown(wait=wait)


class CloudTasksIngestionRunner(IngestionJobRunner):
    """
    Submits ingestion jobs to Google Cloud Tasks for asynchronous execution by worker service.
    Enqueues only metadata descriptor payload to prevent task payload size limits and security leaks.
    """

    def __init__(self, client=None):
        self.client = client

    def submit_ingestion(
        self,
        space_id: str,
        file_id: str,
        content_bytes: bytes,
        filename: str,
        store: MemoryStore,
    ) -> str:
        import json

        dispatcher_id = f"dispatcher_{uuid.uuid4().hex[:8]}"
        # 1. Acquire ingestion lease
        acquired, file_rec, target_gen = store.acquire_ingestion_lease(
            space_id=space_id,
            file_id=file_id,
            worker_id=dispatcher_id,
            lease_seconds=300,
        )
        if not acquired or not file_rec:
            f = store.get_file(file_id)
            if f and f.active_generation and f.active_generation >= 1 and f.ingestion_status in (IngestionStatus.READY, IngestionStatus.READY_PARTIAL):
                logger.info(f"File {file_id} already ingested at active generation {f.active_generation}.")
                return f.ingestion_job_id or f"job_ct_{file_id}"
            raise IngestionDispatchError(f"Failed to acquire ingestion lease for file {file_id}")

        job_id = file_rec.ingestion_job_id

        # 2. Build Cloud Tasks HTTP request payload
        try:
            tasks_client = self.client
            if tasks_client is None:
                from google.cloud import tasks_v2
                tasks_client = tasks_v2.CloudTasksClient()

            project = settings.STUDIO_TOWER_CLOUD_TASKS_PROJECT or "studiotower-dev"
            location = settings.STUDIO_TOWER_CLOUD_TASKS_LOCATION or "us-central1"
            queue_name = settings.STUDIO_TOWER_CLOUD_TASKS_QUEUE or "studiotower-ingestion-queue"
            parent = tasks_client.queue_path(project, location, queue_name)

            worker_url = f"{settings.STUDIO_TOWER_WORKER_SERVICE_URL.rstrip('/')}/v1/internal/tasks/ingest-document"
            task_payload = {
                "space_id": space_id,
                "file_id": file_id,
                "filename": filename,
                "target_generation": target_gen,
                "job_id": job_id,
            }

            headers = {
                "Content-Type": "application/json",
                "X-Task-Secret": settings.STUDIO_TOWER_TASK_SECRET,
            }

            task = {
                "http_request": {
                    "http_method": 1,  # tasks_v2.HttpMethod.POST
                    "url": worker_url,
                    "headers": headers,
                    "body": json.dumps(task_payload).encode("utf-8"),
                }
            }
            tasks_client.create_task(request={"parent": parent, "task": task})
            logger.info(f"Enqueued Cloud Task for file {file_id} (job {job_id}) on queue {queue_name}")
            return job_id

        except Exception as e:
            logger.exception(f"Failed to enqueue Cloud Task for file {file_id}: {e}")
            store.fail_ingestion_generation(
                space_id=space_id,
                file_id=file_id,
                expected_job_id=job_id,
                error_code="ERR_CLOUD_TASKS_ENQUEUE_FAILED",
                error_msg=f"Failed to dispatch to Cloud Tasks: {str(e)}",
            )
            raise IngestionDispatchError(f"Failed to enqueue Cloud Task for file {file_id}: {e}") from e

    def recover_expired_leases(self, store: MemoryStore) -> list[FileRecord]:
        reclaimed = store.scan_and_reclaim_expired_ingestion_leases()
        successful = []
        for f in reclaimed:
            try:
                self.submit_ingestion(f.space_id, f.file_id, b"", f.filename, store)
                successful.append(f)
            except Exception as e:
                logger.warning(f"Failed to re-dispatch reclaimed file {f.file_id}: {e}")
        return successful


_runner_instance: Optional[IngestionJobRunner] = None
_runner_lock = threading.Lock()


def get_ingestion_runner() -> IngestionJobRunner:
    """Returns the managed singleton IngestionJobRunner configured by application settings."""
    global _runner_instance
    with _runner_lock:
        if _runner_instance is None:
            runner_type = settings.INGESTION_RUNNER.lower()
            if runner_type == "inline":
                _runner_instance = InlineIngestionRunner()
            elif runner_type == "cloud_tasks":
                _runner_instance = CloudTasksIngestionRunner()
            else:
                _runner_instance = ThreadPoolIngestionRunner(max_workers=4)
        return _runner_instance


def set_ingestion_runner(runner: Optional[IngestionJobRunner]) -> None:
    """Allows test fixtures or lifecycle hooks to set or reset the runner instance."""
    global _runner_instance
    with _runner_lock:
        if _runner_instance and isinstance(_runner_instance, ThreadPoolIngestionRunner) and _runner_instance != runner:
            _runner_instance.shutdown(wait=False)
        _runner_instance = runner


def shutdown_ingestion_runner() -> None:
    """Shuts down active runners (e.g. at FastAPI lifespan termination)."""
    set_ingestion_runner(None)
