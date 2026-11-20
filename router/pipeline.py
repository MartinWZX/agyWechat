"""Two-State Deterministic Gateway Router (Outer Router State -> Inner Engine Passthrough)."""
from typing import Any, Dict, Optional

from memory.core_state_store import CoreStateStore
from router.engine_session_router import EngineSessionRouter, RouteDecision


class DeterministicRouter:
    """Routes Non-Engine text commands cleanly and passes In-Engine inputs straight to native CLIs."""

    def __init__(
        self,
        store: CoreStateStore,
        hub: Any = None,
        config: Optional[Dict[str, Any]] = None,
        settings: Any = None,
        health_monitor: Any = None,
    ):
        self.store = store
        self.hub = hub
        self.config = config or {}
        self.settings = settings
        self.health_monitor = health_monitor
        self.engine_router = EngineSessionRouter(
            store=store,
            hub=hub,
            config=config,
            settings=settings,
            health_monitor=health_monitor,
        )

    async def route(
        self,
        user_id: str,
        text: str,
        session_state: Optional[Dict[str, Any]] = None,
    ) -> RouteDecision:
        """Route message strictly according to Non-Engine vs In-Engine FSM state."""
        t = (text or "").strip()
        state = session_state if session_state is not None else self.engine_router.get_session_state(user_id)
        active_engine = str(state.get("active_engine") or "")

        if not active_engine:
            return await self.engine_router.route_non_engine(user_id, t)

        engine_decision = await self.engine_router.route_in_engine(user_id, t, state)
        if engine_decision is not None:
            return engine_decision

        meta = state.get("meta") or {}
        ws = state.get("workspace_dir") or self.engine_router.workspace_nav.get_workspace(user_id)
        drv = self.engine_router._get_driver(active_engine)
        return RouteDecision(
            handled=False,
            tier=f"T3_ENGINE_PASSTHROUGH:{active_engine}",
            preferred_engine=active_engine,
            effective_query=t,
            base_query=t,
            strict_engine_pin=True,
            is_engine_slash=t.startswith("/"),
            workspace_dir=ws,
            conversation_id=str(meta.get("conversation_id") or ""),
            session_started_at=float(meta.get("session_started_at") or 0.0),
            supports_native_session=bool(getattr(drv, "supports_native_session", False)),
            cli_flags=self.engine_router.build_cli_flags(active_engine, meta),
        )
