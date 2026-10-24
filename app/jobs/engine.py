from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from typing import Optional

from app.api.credential_pool import CredentialPool
from app.api.exceptions import AnalysisCancelledError, GMGNAuthError, GMGNRateLimitError
from app.browser.exceptions import BrowserNetworkError, BrowserSessionError
from app.api.gmgn_client import GMGNClient
from app.api.rate_limiter import WeightedRateLimiter
from app.api.request_executor import ApiRequestExecutor
from app.api.scheduler import CredentialScheduler, GlobalSafetyController
from app.config import AppConfig
from app.domain.enums import CollectionMode, TaskStatus
from app.domain.models import WalletAnalysisRequest, WalletReport
from app.jobs.event_bus import AnalysisEventBus
from app.jobs.models import AnalysisJob, JobState, WalletTask
from app.providers.orchestrator import DataOrchestrator
from app.services.export_service import ExportService
from app.services.wallet_analysis_service import WalletAnalysisService
from app.storage.database import Database
from app.storage.repositories import Repositories
from app.utils.logger import get_logger
from app.utils.time_utils import format_clock, now_ts

logger = get_logger("gmgn.jobs")


def assign_task_ids(requests: list[WalletAnalysisRequest], job_id: str) -> None:
    for req in requests:
        req.job_id = job_id
        req.wallet_task_id = f"{job_id}:{req.wallet_address}"
        req.task_id = req.wallet_task_id


class AnalysisJobEngine:
    def __init__(
        self,
        config: AppConfig,
        db: Database,
        limiter: WeightedRateLimiter,
        credentials: CredentialPool,
        event_bus: AnalysisEventBus,
        scheduler: Optional[CredentialScheduler] = None,
    ) -> None:
        self.config = config
        self.db = db
        self.repos = Repositories(db)
        self.limiter = limiter
        self.credentials = credentials
        self.bus = event_bus
        self.executor = ApiRequestExecutor(config.api_workers)
        self.scheduler = scheduler
        self._jobs: dict[str, AnalysisJob] = {}
        self._lock = threading.Lock()
        self.fair_token_order: list[str] = []

    def submit(self, requests: list[WalletAnalysisRequest], cancel_event: threading.Event, page: str = "analysis") -> str:
        job_id = str(uuid.uuid4())
        assign_task_ids(requests, job_id)
        job = AnalysisJob(
            job_id=job_id,
            wallets=[r.wallet_address for r in requests],
            requests=requests,
            created_at=datetime.now(),
            state=JobState.QUEUED,
            page=page,
            options=requests[0].options if requests else None,  # type: ignore
            wallet_tasks={
                r.wallet_address: WalletTask(wallet=r.wallet_address, wallet_task_id=r.wallet_task_id) for r in requests
            },
        )
        with self._lock:
            self._jobs[job_id] = job
        thread = threading.Thread(target=self._run, args=(job, cancel_event), daemon=True, name=f"job-{job_id[:8]}")
        thread.start()
        return job_id

    def cancel(self, job_id: str, cancel_event: threading.Event) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.state = JobState.CANCELLING
        cancel_event.set()

    def get_status(self, job_id: str) -> Optional[AnalysisJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def run_blocking(self, requests: list[WalletAnalysisRequest], cancel_event: threading.Event, page: str = "analysis") -> list[WalletReport]:
        job_id = requests[0].job_id if requests and requests[0].job_id else str(uuid.uuid4())
        assign_task_ids(requests, job_id)
        job = AnalysisJob(
            job_id=job_id,
            wallets=[r.wallet_address for r in requests],
            requests=requests,
            created_at=datetime.now(),
            state=JobState.RUNNING,
            page=page,
            wallet_tasks={
                r.wallet_address: WalletTask(wallet=r.wallet_address, wallet_task_id=r.wallet_task_id) for r in requests
            },
        )
        with self._lock:
            self._jobs[job.job_id] = job
        self._run(job, cancel_event)
        return job.reports

    def _emit(self, job: AnalysisJob, payload: dict) -> None:
        event = {
            "job_id": job.job_id,
            "page": job.page,
            "target": job.page,
            "wallet_task_id": payload.get("wallet_task_id") or "",
            "wallet_address": payload.get("wallet") or payload.get("wallet_address") or "",
            "token_address": payload.get("token") or payload.get("token_address") or "",
            **payload,
        }
        self.bus.emit(event)

    def _collection_mode(self, job: AnalysisJob) -> str:
        options = job.requests[0].options if job.requests else None
        raw = str(getattr(options, "collection_mode", "") or "").strip().lower()
        if raw in {CollectionMode.AUTOMATION.value, CollectionMode.API.value}:
            return raw
        cfg = str(getattr(self.config, "collection_mode", "") or "").strip().lower()
        if cfg in {CollectionMode.AUTOMATION.value, CollectionMode.API.value}:
            return cfg
        return CollectionMode.API.value

    def _make_client(self, job: AnalysisJob, cancel_event: threading.Event):
        if self._collection_mode(job) == CollectionMode.AUTOMATION.value:
            from app.browser.cookie_pool import CookiePool
            from app.browser.web_client import GmgnWebClient

            first = job.requests[0] if job.requests else None
            headed = True if first is None else bool(getattr(first.options, "browser_headed", True))
            timeout = float(getattr(self.config, "gmgn_qr_login_timeout", 180) or 180)

            def on_event(message: str, **extra) -> None:
                self._emit(
                    job,
                    {
                        "type": "progress",
                        "message": message,
                        "stage": extra.get("stage") or "browser",
                        "wallet": extra.get("wallet") or (first.wallet_address if first else ""),
                        "done": int(extra.get("done") or 0),
                        "total": int(extra.get("total") or 0),
                        "token": extra.get("token") or "",
                    },
                )

            return GmgnWebClient(
                CookiePool(),
                headed=headed,
                qr_timeout=timeout,
                limiter=self.limiter,
                cancel_event=cancel_event,
                on_event=on_event,
                start_ts=int(first.start_time) if first else 0,
                end_ts=int(first.end_time) if first else 0,
            )

        def on_wait(payload: dict) -> None:
            job.state = JobState.WAITING_API
            resume = payload.get("resume_at") or 0
            remaining = max(0, int(resume - time.time())) if resume else 0
            self._emit(
                job,
                {
                    "type": "WALLET_WAITING_API",
                    "message": f"API 限流，等待恢复（约 {remaining} 秒）",
                    "resume_at": resume,
                    "severity": "RATE_LIMITED",
                },
            )

        return GMGNClient(
            api_key=self.config.api_key,
            base_url=self.config.api_base,
            limiter=self.limiter,
            max_429_retries=self.config.rate_limit_max_retries,
            cancel_event=cancel_event,
            credential_pool=self.credentials,
            on_state=lambda state: self._emit(job, {"type": "api_state", "state": state.display}),
            on_wait=on_wait,
            safety_margin=getattr(self.config, "reset_safety_margin", 3.0),
            shared_limit_detection=getattr(self.config, "shared_limit_detection", True),
            max_api_wait=getattr(self.config, "max_api_wait", 900.0),
        )

    def _run(self, job: AnalysisJob, cancel_event: threading.Event) -> None:
        started = time.time()
        job.state = JobState.RUNNING
        self._emit(job, {"type": "JOB_STARTED", "wallets": job.wallets, "wallet_count": len(job.wallets)})
        client = self._make_client(job, cancel_event)
        scheduler = getattr(client, "scheduler", None)
        self.scheduler = scheduler
        automation = self._collection_mode(job) == CollectionMode.AUTOMATION.value

        def on_health(rows) -> None:
            text = "  |  ".join(
                f"{r.name}: {r.health.value if hasattr(r.health, 'value') else r.health}" for r in rows
            )
            self._emit(job, {"type": "PROVIDER_HEALTH", "message": text, "providers": [
                {"name": r.name, "health": r.health.value if hasattr(r.health, "value") else str(r.health), "detail": r.detail, "optional": r.optional}
                for r in rows
            ]})

        orchestrator = DataOrchestrator.from_config(self.config, client, self.db, cancel_event, on_health=on_health)
        service = WalletAnalysisService(
            client=client,
            db=self.db,
            cancel_event=cancel_event,
            on_progress=lambda payload: self._on_progress(job, payload),
            token_ttl=self.config.token_info_ttl_seconds,
            pool_ttl=self.config.token_pool_ttl_seconds,
            token_workers=self.config.api_workers,
            orchestrator=orchestrator,
            deep_gmgn_history=bool(getattr(self.config, "enable_gmgn_deep_history_fallback", False)),
        )
        try:
            if automation:
                logger.info("采集方式=自动化，立刻启动浏览器（不先跑 Helius）")
                warmup = getattr(client, "warmup", None)
                if warmup:
                    warmup()
            for request in job.requests:
                self.repos.save_wallet_task(
                    request.wallet_task_id,
                    job.job_id,
                    request.wallet_address,
                    JobState.QUEUED.value,
                )
            if automation or len(job.requests) <= 1:
                for request in job.requests:
                    self._analyze_wallet(job, service, scheduler, request, cancel_event)
            else:
                self._analyze_wallets_fair(job, service, scheduler, cancel_event)
            if self.config.export_combined and len(job.reports) > 1:
                ExportService().export_batch(job.reports, job.job_id)
            job.state = JobState.SUCCESS if job.reports else JobState.FAILED
            if any(r.status == TaskStatus.PARTIAL for r in job.reports) or (job.reports and len(job.reports) < len(job.requests)):
                job.state = JobState.PARTIAL
            elapsed = time.time() - started
            self.repos.save_job(
                job.job_id,
                job.wallets,
                "single" if len(job.wallets) <= 1 else "batch",
                job.state.value,
                {
                    "wallet_count": len(job.wallets),
                    "token_count": sum(len(r.tokens) for r in job.reports),
                    "excel_path": job.reports[-1].excel_path if job.reports else "",
                },
                job.reports[-1].excel_path if job.reports else "",
                elapsed,
                api_requests=client.request_count,
                cache_hits=service.cache.hits,
            )
            self._write_perf(job, client, service, elapsed)
            self._emit(job, {"type": "done", "reports": job.reports})
        except AnalysisCancelledError:
            job.state = JobState.CANCELLED
            self._emit(job, {"type": "cancelled", "reports": job.reports})
        except GMGNAuthError as exc:
            job.state = JobState.FAILED
            job.error = str(exc)
            self._emit(job, {"type": "error", "message": str(exc), "reports": job.reports})
        except BrowserNetworkError as exc:
            job.state = JobState.FAILED
            job.error = str(exc)
            logger.error("%s", exc)
            self._emit(job, {"type": "error", "message": str(exc), "reports": job.reports})
        except BrowserSessionError as exc:
            job.state = JobState.FAILED
            job.error = str(exc)
            logger.error("%s", exc)
            self._emit(job, {"type": "error", "message": str(exc), "reports": job.reports})
        except Exception as exc:
            if "has been closed" in str(exc).lower() or "TargetClosed" in type(exc).__name__:
                msg = "浏览器窗口被关掉了，采集中断"
                job.state = JobState.FAILED
                job.error = msg
                logger.error("%s", msg)
                self._emit(job, {"type": "error", "message": msg, "reports": job.reports})
            else:
                logger.exception("Job 失败")
                job.state = JobState.FAILED
                job.error = str(exc)
                self._emit(job, {"type": "error", "message": str(exc), "reports": job.reports})
        finally:
            closer = getattr(client, "close", None)
            if closer:
                try:
                    closer()
                except Exception:
                    logger.debug("关闭采集客户端失败", exc_info=True)

    def _on_progress(self, job: AnalysisJob, payload: dict) -> None:
        wallet = payload.get("wallet") or ""
        if payload.get("stage") == "token" and wallet:
            self.fair_token_order.append(wallet)
            task = job.wallet_tasks.get(wallet)
            if task:
                task.token_completed = int(payload.get("done") or task.token_completed)
                task.token_total = int(payload.get("total") or task.token_total)
                task.current_stage = "First Buy"
        self._emit(job, {"type": "progress", **payload})

    def _guard_api(self, job: AnalysisJob, task: WalletTask, scheduler: CredentialScheduler, cancel_event: threading.Event, service: WalletAnalysisService | None = None) -> None:
        if getattr(getattr(service, "client", None), "cookie_mode", False):
            return
        if scheduler is None:
            return
        orch = getattr(service, "orchestrator", None) if service else None
        if orch is not None:
            helius = orch.provider("helius")
            if helius is not None and getattr(helius, "enabled", False):
                return
            moralis = orch.provider("moralis")
            if moralis is not None and moralis.enabled:
                return
            rpc = orch.provider("solana_rpc")
            if rpc is not None and getattr(rpc, "enabled", False):
                return
        if scheduler.pool.has_healthy_now() and scheduler.safety.global_cooldown_until <= time.time():
            return
        task.state = JobState.WAITING_API
        job.state = JobState.WAITING_API
        resume = scheduler.get_next_available_time()
        remaining = max(0, int(resume - time.time()))
        self._emit(
            job,
            {
                "type": "WALLET_WAITING_API",
                "wallet": task.wallet,
                "wallet_task_id": task.wallet_task_id,
                "message": f"等待 API Key 冷却，剩余 {remaining} 秒",
                "resume_at": resume,
                "severity": "RATE_LIMITED",
            },
        )
        self.repos.save_wallet_task(task.wallet_task_id, job.job_id, task.wallet, JobState.WAITING_API.value, current_stage="API Cooldown")
        scheduler.wait_for_available_credential(cancel_event)
        task.state = JobState.RUNNING
        job.state = JobState.RUNNING
        self._emit(job, {"type": "KEY_RECOVERED", "wallet": task.wallet, "wallet_task_id": task.wallet_task_id})

    def _analyze_wallet(
        self,
        job: AnalysisJob,
        service: WalletAnalysisService,
        scheduler: CredentialScheduler,
        request: WalletAnalysisRequest,
        cancel_event: threading.Event,
        max_rounds: int = 20,
    ) -> Optional[WalletReport]:
        task = job.wallet_tasks[request.wallet_address]
        for _ in range(max_rounds):
            self._check(cancel_event)
            self._guard_api(job, task, scheduler, cancel_event, service)
            task.state = JobState.RUNNING
            task.started_at = task.started_at or time.time()
            self._emit(job, {"type": "WALLET_STARTED", "wallet": request.wallet_address, "wallet_task_id": request.wallet_task_id})
            try:
                report = service.analyze(request)
                with self._lock:
                    job.reports.append(report)
                task.state = JobState.SUCCESS
                task.token_total = len(report.tokens)
                task.token_completed = len(report.tokens)
                task.finished_at = time.time()
                task.api_requests = report.api_stats.requests
                task.cache_hits = report.api_stats.cache_hits
                task.count_429 = report.api_stats.retries_429
                self.repos.save_wallet_task(
                    request.wallet_task_id,
                    job.job_id,
                    request.wallet_address,
                    JobState.SUCCESS.value,
                    token_total=task.token_total,
                    token_completed=task.token_completed,
                    finished_at=task.finished_at,
                    api_requests=task.api_requests,
                    cache_hits=task.cache_hits,
                )
                self._emit(job, {"type": "wallet_done", "report": report, "wallet": request.wallet_address, "wallet_task_id": request.wallet_task_id})
                return report
            except AnalysisCancelledError:
                task.state = JobState.CANCELLED
                raise
            except GMGNRateLimitError as exc:
                task.state = JobState.WAITING_API
                task.error = str(exc)
                logger.warning("钱包 %s WAITING_API: %s", request.wallet_address, exc)
                self._emit(
                    job,
                    {
                        "type": "RATE_LIMITED",
                        "wallet": request.wallet_address,
                        "wallet_task_id": request.wallet_task_id,
                        "message": "API 限流，等待恢复",
                        "severity": "RATE_LIMITED",
                        "reset_at": exc.reset_at,
                    },
                )
                continue
            except GMGNAuthError:
                task.state = JobState.FAILED
                raise
            except BrowserSessionError as exc:
                task.state = JobState.FAILED
                task.error = str(exc)
                logger.error("钱包失败 %s: %s", request.wallet_address, exc)
                self._emit(job, {"type": "WALLET_FINISHED", "wallet": request.wallet_address, "error": str(exc), "wallet_task_id": request.wallet_task_id})
                if "关掉" in str(exc) or "采集中断" in str(exc):
                    raise
                return None
            except Exception as exc:
                closed = "has been closed" in str(exc).lower() or "TargetClosed" in type(exc).__name__
                if closed:
                    wrapped = BrowserSessionError("浏览器窗口被关掉了，采集中断")
                    task.state = JobState.FAILED
                    task.error = str(wrapped)
                    logger.error("钱包失败 %s: %s", request.wallet_address, wrapped)
                    self._emit(job, {"type": "WALLET_FINISHED", "wallet": request.wallet_address, "error": str(wrapped), "wallet_task_id": request.wallet_task_id})
                    raise wrapped from exc
                task.state = JobState.FAILED
                task.error = str(exc)
                logger.exception("钱包失败 %s", request.wallet_address)
                self._emit(job, {"type": "WALLET_FINISHED", "wallet": request.wallet_address, "error": str(exc), "wallet_task_id": request.wallet_task_id})
                return None
        task.state = JobState.FAILED
        task.error = "等待 API 恢复超时"
        return None

    def _analyze_wallets_fair(
        self,
        job: AnalysisJob,
        service: WalletAnalysisService,
        scheduler: CredentialScheduler,
        cancel_event: threading.Event,
    ) -> None:
        workers = max(1, min(len(job.requests), self.config.api_workers))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gmgn-wallet") as pool:
            futs = [pool.submit(self._analyze_wallet, job, service, scheduler, req, cancel_event) for req in job.requests]
            for fut in futs:
                self._check(cancel_event)
                fut.result()

    def _write_perf(self, job: AnalysisJob, client: GMGNClient, service: WalletAnalysisService, elapsed: float) -> None:
        try:
            import json
            from app.utils.paths import LOG_DIR, ensure_runtime_dirs

            ensure_runtime_dirs()
            snap = self.limiter.snapshot() if self.limiter else client.limiter.snapshot()
            metrics = getattr(client, "metrics", None)
            payload = {
                "job_id": job.job_id,
                "wallets": job.wallets,
                "elapsed_seconds": elapsed,
                "api_requests": client.request_count,
                "cache_hits": service.cache.hits,
                "cache_misses": service.cache.misses,
                "429": client.retry_429_count,
                "server_rate": snap.server_rate,
                "target": snap.target_utilization,
                "adaptive_target": snap.adaptive_target,
                "effective_rate": snap.effective_rate,
                "utilization_1m": snap.utilization_1m,
                "metrics": metrics.snapshot(snap.server_rate) if metrics else {},
                "keys": self.credentials.snapshot(),
                "safety": client.scheduler.safety.snapshot(),
                "fair_token_order_head": self.fair_token_order[:40],
            }
            path = LOG_DIR / f"performance_{job.job_id}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("写入 performance json 失败")

    def _check(self, cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise AnalysisCancelledError()
