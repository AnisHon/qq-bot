from __future__ import annotations

import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.models import SearchResult, GroupMessageEvent
from app.tools.agent import TOOL_REGISTRY
from app.tools.cache import ToolResultCache
from app.tools.contracts import PolicySnapshot, ToolAccess, ToolContext, ToolResult, ToolStatus
from app.tools.policies import group_member_policy_snapshot
from app.tools.web_research import WebRequestBudget


class ToolRegistryCompatibilityTest(IsolatedAsyncioTestCase):
    def context(self, *, result_max_chars=2000, **extensions):
        return ToolContext(
            request_id="compat-request",
            group_id="group-1",
            user_id="user-1",
            policy_snapshot=PolicySnapshot(roles=frozenset({"group_member"})),
            deadline_monotonic=asyncio.get_running_loop().time() + 2,
            tool_timeout_seconds=1,
            result_max_chars=result_max_chars,
            extensions=extensions,
        )

    async def test_registry_exposes_one_web_search_surface_and_separate_github_tools(self) -> None:
        expected = {
            "knowledge_search": ("_knowledge_search", ToolAccess.LOCAL_READ),
            "web_search": ("_web_search", ToolAccess.NETWORK_READ),
            "news_search": ("_news_search", ToolAccess.NETWORK_READ),
            "web_read": ("_web_read", ToolAccess.NETWORK_READ),
            "github_repo_search": ("_github_repo_search", ToolAccess.NETWORK_READ),
            "github_repo_inspect": ("_github_repo_inspect", ToolAccess.NETWORK_READ),
            "github_read_file": ("_github_read_file", ToolAccess.NETWORK_READ),
            "weather": ("_weather", ToolAccess.NETWORK_READ),
            "weather_batch": ("_weather_batch", ToolAccess.NETWORK_READ),
            "recipe_lookup": ("_recipe_lookup", ToolAccess.LOCAL_READ),
            "recipe_machine_lookup": ("_recipe_machine_lookup", ToolAccess.LOCAL_READ),
            "sfm_examples": ("_sfm_examples", ToolAccess.LOCAL_READ),
            "sfm_generate_validate": ("_sfm_generate_validate", ToolAccess.VALIDATION_PROCESS),
        }
        self.assertEqual(
            TOOL_REGISTRY.names(),
            frozenset(expected) | {"repository_search", "repository_read", "task_evidence_read"},
        )
        self.assertIsNone(TOOL_REGISTRY.get("multi_search"))
        self.assertIsNone(TOOL_REGISTRY.get("fetch_url"))
        for name, (handler_name, access) in expected.items():
            spec = TOOL_REGISTRY.get(name)
            self.assertEqual(spec.handler.__name__, handler_name)
            self.assertEqual(spec.policy.access, access)
            self.assertEqual(spec.timeout_seconds, 15 if name.startswith(("recipe_", "sfm_")) else 60)
            self.assertEqual(spec.max_concurrency, 2 if name == "sfm_generate_validate" else 4)
            self.assertEqual(spec.output_kind.value, "evidence")

    async def test_existing_github_weather_and_local_knowledge_tools_still_dispatch(self) -> None:
        rows = [SearchResult("source", "verified fixture result", "https://example.test/source", "fixture")]
        cache = ToolResultCache()
        test_cases = [
            ("github_repo_search", {"query": "q"}, "app.tools.legacy_tools.search_github_repositories", ("q",)),
            (
                "github_repo_inspect", {"repository": "owner/repo", "query": "q"},
                "app.tools.legacy_tools.inspect_github_repository", ("owner/repo", "q"),
            ),
            (
                "github_read_file", {"repository": "owner/repo", "path": "README.md"},
                "app.tools.legacy_tools.read_github_file", ("owner/repo", "README.md"),
            ),
            ("weather", {"location": "Tokyo"}, "app.tools.legacy_tools.get_weather", ("Tokyo",)),
        ]

        with patch("app.tools.legacy_tools.get_cache", return_value=cache):
            for name, arguments, target, expected_args in test_cases:
                with self.subTest(name=name):
                    with patch(target, new=AsyncMock(return_value=rows)) as operation:
                        result = await TOOL_REGISTRY.dispatch(name, arguments, self.context())
                    self.assertEqual(result.status, ToolStatus.OK)
                    self.assertEqual(len(result.evidence), 1)
                    operation.assert_awaited_once()
                    self.assertEqual(operation.await_args.args[:len(expected_args)], expected_args)
                    self.assertLessEqual(operation.await_args.kwargs["timeout"], 1)

            with patch("app.tools.legacy_tools.get_weather", new=AsyncMock(return_value=rows)) as weather:
                result = await TOOL_REGISTRY.dispatch(
                    "weather_batch", {"locations": ["Tokyo", "Osaka"]}, self.context()
                )
            self.assertEqual(result.status, ToolStatus.OK)
            self.assertEqual(weather.await_count, 2)

        with patch("app.tools.legacy_tools.get_cache", return_value=cache):
            callback = AsyncMock(return_value=rows)
            result = await TOOL_REGISTRY.dispatch(
                "knowledge_search", {"query": "local"}, self.context(local_knowledge_search=callback)
            )
        self.assertEqual(result.status, ToolStatus.OK)
        callback.assert_awaited_once_with("local")

    async def test_web_tools_delegate_to_the_single_request_scoped_service(self) -> None:
        result = ToolResult(ToolStatus.OK, {"text": "fixture web result"})
        service = type("FakeWebResearchService", (), {})()
        service.search = AsyncMock(return_value=result)
        service.news_search = AsyncMock(return_value=result)
        service.read = AsyncMock(return_value=result)
        budget = WebRequestBudget()
        context = self.context(web_research_service=service, web_research_budget=budget)

        search = await TOOL_REGISTRY.dispatch(
            "web_search", {"query": "GTNH 铂配方", "search_depth": "advanced", "max_results": 4}, context
        )
        news = await TOOL_REGISTRY.dispatch("news_search", {"query": "Tavily news", "time_range": "week"}, context)
        read = await TOOL_REGISTRY.dispatch("web_read", {"url": "https://example.com/article"}, context)

        self.assertEqual([search.status, news.status, read.status], [ToolStatus.OK] * 3)
        service.search.assert_awaited_once()
        self.assertEqual(service.search.await_args.args[:2], ("GTNH 铂配方", context))
        self.assertEqual(service.search.await_args.kwargs["search_depth"], "advanced")
        service.news_search.assert_awaited_once_with(
            "Tavily news", context, max_results=None, time_range="week", start_date=None, end_date=None
        )
        service.read.assert_awaited_once_with("https://example.com/article", context)

    async def test_legacy_weather_alias_and_read_only_github_file_behavior_remain(self) -> None:
        cache = ToolResultCache()
        rows = [SearchResult("weather", "fixture", "", "weather")]
        with patch("app.tools.legacy_tools.get_cache", return_value=cache):
            with patch("app.tools.legacy_tools.get_weather", new=AsyncMock(return_value=rows)) as weather:
                for arguments in ({"query": " Tokyo "}, {"location": " Tokyo "}):
                    result = await TOOL_REGISTRY.dispatch("weather", arguments, self.context())
                    self.assertEqual(result.status, ToolStatus.OK)
                weather.assert_awaited_once()

            with patch("app.tools.legacy_tools.read_github_file", new=AsyncMock(return_value=rows)) as read_file:
                for _ in range(2):
                    await TOOL_REGISTRY.dispatch(
                        "github_read_file", {"repository": "owner/repo", "path": "README.md"}, self.context()
                    )
                self.assertEqual(read_file.await_count, 2)

    async def test_service_exception_is_mapped_without_exposing_provider_text(self) -> None:
        service = type("FailingWebResearchService", (), {})()
        service.search = AsyncMock(side_effect=RuntimeError("sensitive provider payload"))
        result = await TOOL_REGISTRY.dispatch(
            "web_search", {"query": "q"}, self.context(web_research_service=service)
        )
        self.assertEqual(result.status, ToolStatus.ERROR)
        self.assertEqual(result.error.code, "tool_execution_error")
        self.assertNotIn("sensitive provider payload", str(result.content))

    async def test_missing_local_knowledge_is_not_confused_with_permission_denial(self) -> None:
        result = await TOOL_REGISTRY.dispatch("knowledge_search", {"query": "SFM syntax"}, self.context())
        self.assertEqual(result.status, ToolStatus.DATA_UNAVAILABLE)
        self.assertNotEqual(result.status, ToolStatus.DENIED)
        self.assertIn("没有可用的本地知识源", result.error.message)

    def test_group_policy_includes_bot_admin_without_removing_member_role(self) -> None:
        settings = Settings(bot_admin_qq_list=("admin-id",))
        event = GroupMessageEvent("m", "g", "admin-id", "admin", "hi", False, 0, {})
        snapshot = group_member_policy_snapshot(event, settings)
        self.assertEqual(snapshot.roles, frozenset({"group_member", "bot_admin"}))
