from __future__ import annotations

import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.models import SearchResult
from app.models import GroupMessageEvent
from app.tools.agent import TOOL_REGISTRY
from app.tools.cache import ToolResultCache
from app.tools.contracts import PolicySnapshot, ToolAccess, ToolContext, ToolStatus
from app.tools.policies import group_member_policy_snapshot


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

    async def test_each_old_tool_name_has_a_direct_registered_handler_and_access_policy(self) -> None:
        expected = {
            "knowledge_search": ("_knowledge_search", ToolAccess.LOCAL_READ),
            "web_search": ("_web_search", ToolAccess.NETWORK_READ),
            "news_search": ("_news_search", ToolAccess.NETWORK_READ),
            "multi_search": ("_multi_search", ToolAccess.NETWORK_READ),
            "github_repo_search": ("_github_repo_search", ToolAccess.NETWORK_READ),
            "github_repo_inspect": ("_github_repo_inspect", ToolAccess.NETWORK_READ),
            "github_read_file": ("_github_read_file", ToolAccess.NETWORK_READ),
            "fetch_url": ("_fetch_url", ToolAccess.NETWORK_READ),
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
        for name, (handler_name, access) in expected.items():
            spec = TOOL_REGISTRY.get(name)
            self.assertEqual(spec.handler.__name__, handler_name)
            self.assertEqual(spec.policy.access, access)
            self.assertEqual(spec.timeout_seconds, 15 if name.startswith(("recipe_", "sfm_")) else 60)
            self.assertEqual(spec.max_concurrency, 2 if name == "sfm_generate_validate" else 4)
            self.assertEqual(spec.output_kind.value, "evidence")

    async def test_every_external_tool_dispatches_to_its_mocked_legacy_service(self) -> None:
        rows = [SearchResult("source", "verified fixture result", "https://example.test/source", "fixture")]
        cache = ToolResultCache()
        test_cases = [
            ("web_search", {"query": "q"}, "app.tools.legacy_tools.search_web", ("q",)),
            ("news_search", {"query": "q"}, "app.tools.legacy_tools.search_news", ("q",)),
            ("multi_search", {"queries": ["q"]}, "app.tools.legacy_tools.multi_search", (["q"],)),
            (
                "github_repo_search",
                {"query": "q"},
                "app.tools.legacy_tools.search_github_repositories",
                ("q",),
            ),
            (
                "github_repo_inspect",
                {"repository": "owner/repo", "query": "q"},
                "app.tools.legacy_tools.inspect_github_repository",
                ("owner/repo", "q"),
            ),
            (
                "github_read_file",
                {"repository": "owner/repo", "path": "README.md"},
                "app.tools.legacy_tools.read_github_file",
                ("owner/repo", "README.md"),
            ),
            (
                "fetch_url",
                {"url": "https://example.test/page"},
                "app.tools.legacy_tools.fetch_web_page",
                ("https://example.test/page",),
            ),
            ("weather", {"location": "Tokyo"}, "app.tools.legacy_tools.get_weather", ("Tokyo",)),
        ]

        with patch("app.tools.legacy_tools.get_cache", return_value=cache):
            for name, arguments, target, expected_args in test_cases:
                with self.subTest(name=name):
                    with patch(target, new=AsyncMock(return_value=rows)) as operation:
                        result = await TOOL_REGISTRY.dispatch(name, arguments, self.context())
                    self.assertEqual(result.status, ToolStatus.OK)
                    self.assertEqual(result.content["text"].splitlines()[0], "1. source")
                    self.assertEqual(len(result.evidence), 1)
                    operation.assert_awaited_once()
                    actual_args = operation.await_args.args
                    self.assertEqual(actual_args[: len(expected_args)], expected_args)
                    self.assertGreater(operation.await_args.kwargs["timeout"], 0)
                    self.assertLessEqual(operation.await_args.kwargs["timeout"], 1)

            with patch("app.tools.legacy_tools.get_weather", new=AsyncMock(return_value=rows)) as weather:
                result = await TOOL_REGISTRY.dispatch(
                    "weather_batch",
                    {"locations": ["Tokyo", "Osaka"]},
                    self.context(),
                )
            self.assertEqual(result.status, ToolStatus.OK)
            self.assertEqual(weather.await_count, 2)

        with patch("app.tools.legacy_tools.get_cache", return_value=cache):
            callback = AsyncMock(return_value=rows)
            result = await TOOL_REGISTRY.dispatch(
                "knowledge_search",
                {"query": "local"},
                self.context(local_knowledge_search=callback),
            )
        self.assertEqual(result.status, ToolStatus.OK)
        callback.assert_awaited_once_with("local")

    async def test_cacheable_weather_uses_compat_alias_and_bypasses_cache_for_reads(self) -> None:
        cache = ToolResultCache()
        rows = [SearchResult("weather", "fixture", "", "weather")]
        with patch("app.tools.legacy_tools.get_cache", return_value=cache):
            with patch("app.tools.legacy_tools.get_weather", new=AsyncMock(return_value=rows)) as weather:
                for arguments in ({"query": " Tokyo "}, {"location": " Tokyo "}):
                    result = await TOOL_REGISTRY.dispatch("weather", arguments, self.context())
                    self.assertEqual(result.status, ToolStatus.OK)
                weather.assert_awaited_once()

            with patch("app.tools.legacy_tools.fetch_web_page", new=AsyncMock(return_value=rows)) as fetch:
                for _ in range(2):
                    await TOOL_REGISTRY.dispatch(
                        "fetch_url", {"url": "https://example.test/page"}, self.context()
                    )
                self.assertEqual(fetch.await_count, 2)

            with patch("app.tools.legacy_tools.read_github_file", new=AsyncMock(return_value=rows)) as read_file:
                for _ in range(2):
                    await TOOL_REGISTRY.dispatch(
                        "github_read_file",
                        {"repository": "owner/repo", "path": "README.md"},
                        self.context(),
                    )
                self.assertEqual(read_file.await_count, 2)

    async def test_scalar_multi_search_and_weather_batch_parameters_remain_compatible(self) -> None:
        cache = ToolResultCache()
        rows = [SearchResult("source", "fixture", "", "fixture")]
        with patch("app.tools.legacy_tools.get_cache", return_value=cache):
            with patch("app.tools.legacy_tools.multi_search", new=AsyncMock(return_value=rows)) as search:
                result = await TOOL_REGISTRY.dispatch("multi_search", {"queries": "q"}, self.context())
                self.assertEqual(result.status, ToolStatus.OK)
                self.assertEqual(search.await_args.args[0], ["q"])

            with patch("app.tools.legacy_tools.get_weather", new=AsyncMock(return_value=rows)) as weather:
                result = await TOOL_REGISTRY.dispatch("weather_batch", {"locations": "Tokyo"}, self.context())
                self.assertEqual(result.status, ToolStatus.OK)
                weather.assert_awaited_once()
                self.assertEqual(weather.await_args.args[0], "Tokyo")

    async def test_provider_exception_maps_to_safe_tool_error(self) -> None:
        with patch("app.tools.legacy_tools.get_cache", return_value=ToolResultCache()):
            with patch(
                "app.tools.legacy_tools.search_web",
                new=AsyncMock(side_effect=RuntimeError("sensitive provider payload")),
            ):
                result = await TOOL_REGISTRY.dispatch(
                    "web_search", {"query": "q"}, self.context()
                )
        self.assertEqual(result.status, ToolStatus.ERROR)
        self.assertEqual(result.error.code, "tool_execution_error")
        self.assertNotIn("sensitive provider payload", str(result.content))

    async def test_missing_local_knowledge_is_not_confused_with_permission_denial(self) -> None:
        result = await TOOL_REGISTRY.dispatch(
            "knowledge_search",
            {"query": "SFM syntax"},
            self.context(),
        )
        self.assertEqual(result.status, ToolStatus.DATA_UNAVAILABLE)
        self.assertNotEqual(result.status, ToolStatus.DENIED)
        self.assertIn("没有可用的本地知识源", result.error.message)

    def test_group_policy_includes_bot_admin_without_removing_member_role(self) -> None:
        settings = Settings(bot_admin_qq_list=("admin-id",))
        event = GroupMessageEvent("m", "g", "admin-id", "admin", "hi", False, 0, {})
        snapshot = group_member_policy_snapshot(event, settings)
        self.assertEqual(snapshot.roles, frozenset({"group_member", "bot_admin"}))
