"""Тесты второго слоя (jarvis.llm) БЕЗ сети: вместо провайдера — поддельный клиент с заранее заданными ответами.

    python3 -m pytest tests/test_llm.py -q

Проверяется: инструменты выполняются через исполнители первого слоя, результаты возвращаются модели, опасные действия
требуют подтверждения, ошибки инструментов и кривой JSON не роняют агента, подписи Gemini возвращаются в историю,
рассуждения reasoning_content — нет, история разговора сохраняется, диспетчер отдаёт маршрут llm агенту.
"""
import json
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "ml", Path(__file__).resolve().parent):
    sys.path.insert(0, str(p))

from jarvis import dispatcher as assistant   # noqa: E402
from jarvis import llm as llm_agent          # noqa: E402
import ml_spec                      # noqa: E402



class Msg(SimpleNamespace):
    def model_dump(self, exclude_none=False):
        data = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments},
                                   **({"extra_content": c.extra_content} if getattr(c, "extra_content", None) else {})}
                                  for c in self.tool_calls]
        if getattr(self, "reasoning_content", None):
            data["reasoning_content"] = self.reasoning_content
        return {k: v for k, v in data.items() if v is not None} if exclude_none else data


def call(name, args, cid="c1", raw=None, extra=None):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=raw if raw is not None else json.dumps(args, ensure_ascii=False)),
                           extra_content=extra)


def reply(text=None, calls=None, finish="stop", reasoning=None):
    msg = Msg(content=text, tool_calls=calls or None, reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="tool_calls" if calls else finish)])


class FakeClient:
    """Отдаёт ответы по очереди и запоминает, что ему прислали."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs, ensure_ascii=False, default=str)))
        return self.replies.pop(0)


PROVIDER = llm_agent.Provider("test", "http://fake", "fake-model")


def agent(replies, confirm=lambda q: True, dry_run=True):
    client = FakeClient(replies)
    return llm_agent.LLMAgent(client=client, executor=llm_agent.ToolExecutor(dry_run=dry_run, confirm=confirm), provider=PROVIDER), client


class TestTools(unittest.TestCase):
    def test_tool_schema_covers_every_action(self):
        tools = {t["function"]["name"]: t["function"] for t in llm_agent.build_tools()}
        self.assertEqual(set(tools), {"system_action", "open_app", "open_web", "translate", "list_folder", "find_files",
                                      "system_query", "read_text_file", "write_text_file", "run_command", "list_apps"})
        self.assertEqual(tools["system_action"]["parameters"]["properties"]["action"]["enum"], list(ml_spec.SYSTEM_ACTIONS))
        for act in ml_spec.SYSTEM_ACTIONS:
            self.assertIn(act, tools["system_action"]["description"])

    def test_dangerous_actions_exist(self):
        self.assertTrue(llm_agent.DANGEROUS <= set(ml_spec.SYSTEM_ACTIONS))


class TestLoop(unittest.TestCase):
    def test_plain_answer_without_tools(self):
        a, client = agent([reply("Квантовый компьютер считает с помощью кубитов.")])
        self.assertEqual(a.handle("что такое квантовый компьютер"), "Квантовый компьютер считает с помощью кубитов.")
        req = client.requests[0]
        self.assertEqual(req["messages"][0]["role"], "system")
        self.assertEqual(req["model"], "fake-model")
        self.assertEqual(len(req["tools"]), 11)

    def test_tool_call_runs_executor_and_returns_result_to_model(self):
        a, client = agent([
            reply(calls=[call("system_action", {"action": "vol_set", "query": "30", "site": "", "text": ""})]),
            reply("Поставила громкость 30 процентов."),
        ])
        self.assertEqual(a.handle("сделай так чтобы было тише, процентов тридцать"), "Поставила громкость 30 процентов.")
        tool_msg = client.requests[1]["messages"][-1]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertEqual(tool_msg["tool_call_id"], "c1")
        self.assertIn("30%", tool_msg["content"])

    def test_multi_step_chain(self):
        home = Path(tempfile.mkdtemp(prefix="pc_agent_llm_"))
        try:
            (home / "a.pdf").write_bytes(b"x")
            patcher = mock.patch("pathlib.Path.home", return_value=home)       # «домашняя папка» = временная
            patcher.start()
            self.addCleanup(patcher.stop)
            a, client = agent([
                reply(calls=[call("list_folder", {"folder": str(home)})]),
                reply(calls=[call("system_action", {"action": "fs_archive", "query": "a.pdf", "site": "", "text": ""}, cid="c2")]),
                reply("Заархивировала a.pdf."),
            ])
            self.assertEqual(a.handle("заархивируй pdf из этой папки"), "Заархивировала a.pdf.")
            self.assertIn("a.pdf", client.requests[1]["messages"][-1]["content"])
            self.assertEqual(len(client.requests), 3)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_dangerous_action_needs_confirmation(self):
        asked = []
        a, client = agent([
            reply(calls=[call("system_action", {"action": "power_off", "query": "", "site": "", "text": ""})]),
            reply("Хорошо, не выключаю."),
        ], confirm=lambda q: asked.append(q) or False, dry_run=False)
        self.assertEqual(a.handle("выключи всё и пойдём спать"), "Хорошо, не выключаю.")
        self.assertEqual(len(asked), 1)
        self.assertIn("отказался", client.requests[1]["messages"][-1]["content"])

    def test_read_only_info_runs_even_in_dry_run(self):
        a, client = agent([
            reply(calls=[call("system_action", {"action": "sys_time"}), call("system_action", {"action": "vol_set", "query": "30"}, cid="c2")]),
            reply("Готово."),
        ])
        a.handle("который час и сделай громкость 30")
        results = [m["content"] for m in client.requests[1]["messages"] if m["role"] == "tool"]
        self.assertTrue(results[0].startswith("Сейчас"), results[0])       # время прочитано по-настоящему
        self.assertTrue(results[1].startswith("[dry-run]"), results[1])     # а громкость не тронута

    def test_tool_error_and_bad_json_do_not_crash(self):
        a, client = agent([
            reply(calls=[call("system_action", {}, raw="{not json"), call("no_such_tool", {}, cid="c2")]),
            reply("Не получилось, уточните."),
        ])
        self.assertEqual(a.handle("сделай что-нибудь"), "Не получилось, уточните.")
        results = [m for m in client.requests[1]["messages"] if m["role"] == "tool"]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["content"].startswith("Ошибка") for r in results))

    def test_gemini_signature_kept_reasoning_dropped(self):
        a, client = agent([
            reply(calls=[call("system_action", {"action": "sys_time", "query": "", "site": "", "text": ""},
                              extra={"google": {"thought_signature": "SIG"}})], reasoning="долгие рассуждения"),
            reply("Сейчас полдень."),
        ])
        a.handle("который час")
        assistant_msg = client.requests[1]["messages"][2]
        self.assertEqual(assistant_msg["tool_calls"][0]["extra_content"], {"google": {"thought_signature": "SIG"}})
        self.assertNotIn("reasoning_content", assistant_msg)

    def test_history_is_kept_between_phrases(self):
        a, client = agent([reply("Запомнила."), reply("Вас зовут Сергей.")])
        a.handle("меня зовут Сергей")
        a.handle("как меня зовут")
        texts = [m.get("content") for m in client.requests[1]["messages"]]
        self.assertIn("меня зовут Сергей", texts)

    def test_stops_after_too_many_rounds_with_a_summary(self):
        loop = [reply(calls=[call("system_action", {"action": "sys_time", "query": "", "site": "", "text": ""}, cid=f"c{i}")])
                for i in range(llm_agent.MAX_TOOL_ROUNDS)]
        a, client = agent(loop + [reply("Время узнала, а автозапуск так и не нашла.")])
        self.assertEqual(a.handle("крутись бесконечно"), "Время узнала, а автозапуск так и не нашла.")
        self.assertEqual(client.requests[-1]["tool_choice"], "none")       # итог — без инструментов
        self.assertEqual(a.history, [])


class TestAssistantRouting(unittest.TestCase):
    def test_llm_route_goes_to_the_agent(self):
        a, _ = agent([reply("Ответ второго слоя.")])

        def parse(phrase):
            return {"phrase": phrase, "route": "llm", "action": "none", "site": "", "query": "", "text": "", "lang": "none",
                    "confidence": 0.6, "fallback": True, "reason": "not sure about the intent (0.60)"}
        res, plan, msg = assistant.dispatch_phrase("что-то сложное", parser=parse, dry_run=True, agent=a)
        self.assertIsNone(plan)
        self.assertEqual(msg, "Ответ второго слоя.")
        report = assistant.describe_trace(res)
        self.assertIn("ВТОРОМУ СЛОЮ", report)
        self.assertIn("не уверен", report)
        self.assertIn("test/fake-model", report)

    def test_trace_for_first_layer_and_tools(self):
        a, _ = agent([reply(calls=[call("system_action", {"action": "sys_time"})]), reply("Сейчас полдень.")])

        def parse_llm(phrase):
            return {"phrase": phrase, "route": "llm", "action": "none", "confidence": 0.99, "fallback": False, "reason": ""}
        res, _, _ = assistant.dispatch_phrase("сколько до встречи", parser=parse_llm, dry_run=True, agent=a)
        self.assertEqual(len(res["trace"]["tools"]), 1)
        self.assertIn("инструментов вызвано 1", assistant.describe_trace(res))

        def parse_sys(phrase):
            return {"phrase": phrase, "route": "system", "action": "vol_down", "site": "", "query": "", "text": "",
                    "confidence": 0.97, "fallback": False, "reason": ""}
        res, plan, _ = assistant.dispatch_phrase("сделай потише", parser=parse_sys, dry_run=True)
        self.assertIsNotNone(plan)
        self.assertIn("ПЕРВЫЙ СЛОЙ", assistant.describe_trace(res))


# =============================================================================
# Расширенные проверки второго слоя
# =============================================================================
def _http_error(cls, status):
    import httpx
    req = httpx.Request("POST", "http://fake/chat")
    return cls(f"status {status}", response=httpx.Response(status, request=req), body=None)


class RaisingClient(FakeClient):
    def __init__(self, exc):
        super().__init__([])
        self.exc = exc

    def _create(self, **kwargs):
        raise self.exc


class TestDangerousEverywhere(unittest.TestCase):
    """Каждое опасное действие: без «да» не выполняется, с «да» выполняется; в dry-run вопрос не задаётся."""

    def test_each_dangerous_action_asks_and_respects_no(self):
        for action in sorted(llm_agent.DANGEROUS):
            with self.subTest(action=action):
                ran, asked = [], []
                with mock.patch.object(llm_agent.system, "execute_system_plan",
                                       side_effect=lambda plan, dry_run=False: ran.append(plan.action) or "выполнено"):
                    a, client = agent([reply(calls=[call("system_action", {"action": action, "query": "education"})]), reply("Ок.")],
                                      confirm=lambda q: asked.append(q) or False, dry_run=False)
                    a.handle("сделай это")
                self.assertEqual(len(asked), 1)
                self.assertEqual(ran, [])
                self.assertIn("отказался", client.requests[1]["messages"][-1]["content"])

    def test_yes_executes(self):
        ran = []
        with mock.patch.object(llm_agent.system, "execute_system_plan",
                               side_effect=lambda plan, dry_run=False: ran.append(plan.action) or "выполнено"):
            a, _ = agent([reply(calls=[call("system_action", {"action": "power_off"})]), reply("Выключаю.")],
                         confirm=lambda q: True, dry_run=False)
            a.handle("выключи компьютер")
        self.assertEqual(len(ran), 1)

    def test_safe_actions_never_ask(self):
        asked = []
        with mock.patch.object(llm_agent.system, "execute_system_plan", return_value="ок"):
            for action in ("vol_set", "night_light_on", "sys_time", "shot_screen", "fs_copy", "fs_mkdir", "timer_set"):
                a, _ = agent([reply(calls=[call("system_action", {"action": action, "query": "10"})]), reply("Ок.")],
                             confirm=lambda q: asked.append(q) or True, dry_run=False)
                a.handle("x")
        self.assertEqual(asked, [])

    def test_dry_run_never_asks(self):
        asked = []
        a, _ = agent([reply(calls=[call("system_action", {"action": "fs_trash", "query": "education"})]), reply("Ок.")],
                     confirm=lambda q: asked.append(q) or True, dry_run=True)
        a.handle("удали education")
        self.assertEqual(asked, [])


class TestToolArguments(unittest.TestCase):
    def test_unknown_action_and_missing_fields(self):
        a, client = agent([
            reply(calls=[call("system_action", {"action": "launch_rockets"}), call("open_app", {}, cid="c2"),
                         call("open_web", {"mode": "web_search"}, cid="c3"), call("translate", {"text": ""}, cid="c4"),
                         call("find_files", {"pattern": "a"}, cid="c5"), call("find_files", {"pattern": "../etc"}, cid="c6"),
                         call("list_folder", {"folder": "нет_такой_папки_42"}, cid="c7")]),
            reply("Не вышло."),
        ])
        a.handle("сломай всё")
        results = [m["content"] for m in client.requests[1]["messages"] if m["role"] == "tool"]
        self.assertEqual(len(results), 7)
        self.assertTrue(all(r.startswith("Ошибка") for r in results), results)

    def test_args_as_json_array_or_null(self):
        a, client = agent([reply(calls=[call("system_action", None, raw="[1,2]"), call("system_action", None, raw="null", cid="c2")]),
                           reply("Ок.")])
        a.handle("x")
        results = [m["content"] for m in client.requests[1]["messages"] if m["role"] == "tool"]
        self.assertTrue(results[0].startswith("Ошибка"), results)

    def test_tool_output_is_truncated(self):
        with mock.patch.object(llm_agent.ToolExecutor, "_run", return_value="я" * 20000):
            a, client = agent([reply(calls=[call("list_folder", {"folder": "x"})]), reply("Ок.")])
            a.handle("x")
        self.assertLessEqual(len(client.requests[1]["messages"][-1]["content"]), 8000)

    def test_every_tool_runs_in_dry_run(self):
        home = Path(tempfile.mkdtemp(prefix="pc_agent_llm_"))
        try:
            (home / "Desktop").mkdir()
            (home / "Desktop" / "отчёт.pdf").write_bytes(b"x")
            with mock.patch("pathlib.Path.home", return_value=home):
                a, client = agent([
                    reply(calls=[call("system_action", {"action": "vol_set", "query": "30"}),
                                 call("open_app", {"name": "калькулятор"}, cid="c2"),
                                 call("open_web", {"site": "github", "mode": "web_open"}, cid="c3"),
                                 call("translate", {"text": "привет", "lang": "en"}, cid="c4"),
                                 call("list_folder", {"folder": "рабочий стол"}, cid="c5"),
                                 call("find_files", {"pattern": ".pdf"}, cid="c6")]),
                    reply("Готово."),
                ])
                a.handle("всё сразу")
            results = {m["tool_call_id"]: m["content"] for m in client.requests[1]["messages"] if m["role"] == "tool"}
            self.assertIn("30%", results["c1"])
            self.assertTrue(results["c2"].startswith("[dry-run]"), results["c2"])
            self.assertIn("github.com", results["c3"])
            self.assertIn("translate.google", results["c4"])
            self.assertIn("отчёт.pdf", results["c5"])
            self.assertIn("отчёт.pdf", results["c6"])
        finally:
            shutil.rmtree(home, ignore_errors=True)


class TestReadOnlyTools(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="pc_agent_llm_home_"))
        for d in ("Desktop", "Downloads", ".ssh", ".config/pc_agent"):
            (self.home / d).mkdir(parents=True)
        (self.home / ".ssh" / "id_ed25519").write_text("KEY", encoding="utf-8")
        (self.home / ".config" / "pc_agent" / "vertex-sa.json").write_text('{"private_key": "x"}', encoding="utf-8")
        (self.home / "Desktop" / "видимый.txt").write_text("x", encoding="utf-8")
        (self.home / "Desktop" / ".скрытый").write_text("x", encoding="utf-8")
        (self.home / ".gitconfig").write_text("[user]", encoding="utf-8")
        (self.home / "Desktop" / ".api_token").write_text("x", encoding="utf-8")
        p = mock.patch("pathlib.Path.home", return_value=self.home)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_hidden_and_outside_folders_are_not_listed(self):
        for folder in ("~/.ssh", str(self.home / ".ssh"), "~/.config/pc_agent", "/etc", "/", "/root", str(self.home / ".."),
                       "~/../../etc"):
            with self.subTest(folder=folder):
                with self.assertRaises(ValueError):
                    llm_agent._list_folder(folder)

    def test_home_folders_are_listed_without_hidden_files(self):
        out = llm_agent._list_folder("~/Desktop")
        self.assertIn("видимый.txt", out)
        self.assertNotIn(".скрытый", out)

    def test_find_files_skips_hidden_folders(self):
        self.assertNotIn("id_ed25519", llm_agent._find_files("id_ed"))
        self.assertNotIn("vertex-sa.json", llm_agent._find_files("vertex"))
        self.assertIn("видимый.txt", llm_agent._find_files("видимый"))
        self.assertNotIn(".gitconfig", llm_agent._find_files("config"))
        self.assertNotIn(".api_token", llm_agent._find_files("token"))
        self.assertNotIn(".скрытый", llm_agent._find_files("скрыт"))
        for bad in ("", "a", "../etc", "/etc/passwd"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                llm_agent._find_files(bad)


class TestProviderErrors(unittest.TestCase):
    def check(self, exc, words):
        a = llm_agent.LLMAgent(client=RaisingClient(exc), executor=llm_agent.ToolExecutor(dry_run=True), provider=PROVIDER)
        with self.assertRaises(llm_agent.LLMUnavailable) as ctx:
            a.handle("привет")
        self.assertTrue(any(w in str(ctx.exception) for w in words), str(ctx.exception))

    def test_http_errors_become_readable_reasons(self):
        import openai
        self.check(_http_error(openai.AuthenticationError, 401), ["ключ"])
        self.check(_http_error(openai.RateLimitError, 429), ["лимит"])
        self.check(_http_error(openai.APIStatusError, 402), ["баланс"])
        self.check(_http_error(openai.APIStatusError, 500), ["500"])
        self.check(_http_error(openai.APIStatusError, 503), ["503"])

    def test_network_error(self):
        import httpx
        import openai
        self.check(openai.APIConnectionError(request=httpx.Request("POST", "http://fake")), ["связи"])

    def test_google_auth_error(self):
        from google.auth.exceptions import RefreshError
        self.check(RefreshError("invalid_grant: account disabled"), ["сервисный аккаунт"])

    def test_unknown_error_is_not_swallowed(self):
        a = llm_agent.LLMAgent(client=RaisingClient(KeyError("bug")), executor=llm_agent.ToolExecutor(dry_run=True), provider=PROVIDER)
        with self.assertRaises(KeyError):
            a.handle("привет")


class TestConversation(unittest.TestCase):
    def test_hint_from_first_layer_reaches_the_model(self):
        a, client = agent([reply("Ок.")])
        a.handle("сделай что-то", hint="not sure about the intent (0.55)")
        self.assertIn("0.55", client.requests[0]["messages"][-1]["content"])

    def test_truncated_answer_is_marked(self):
        a, _ = agent([reply("Длинный ответ", finish="length")])
        self.assertIn("обрезан", a.handle("расскажи всё"))

    def test_empty_answer_becomes_done(self):
        a, _ = agent([reply("")])
        self.assertEqual(a.handle("x"), "Готово.")

    def test_idle_resets_history(self):
        a, client = agent([reply("1"), reply("2")])
        a.handle("первая")
        a.last_used -= llm_agent.HISTORY_IDLE_SECONDS + 1
        a.handle("вторая")
        users = [m["content"] for m in client.requests[1]["messages"] if m["role"] == "user"]
        self.assertEqual(users, ["вторая"])

    def test_long_history_is_trimmed_not_wiped(self):
        hist = []
        for i in range(50):
            hist += [{"role": "user", "content": f"u{i}"}, {"role": "assistant", "content": f"a{i}"}]
        trimmed = llm_agent._trim_history(hist, limit=20)
        self.assertLessEqual(len(trimmed), 20)
        self.assertEqual(trimmed[0]["role"], "user")
        self.assertEqual(trimmed[-1]["content"], "a49")

    def test_trim_never_starts_with_orphan_tool_result(self):
        hist = [{"role": "user", "content": "u"}, {"role": "assistant", "content": "", "tool_calls": [{"id": "c"}]},
                {"role": "tool", "tool_call_id": "c", "content": "r"}, {"role": "assistant", "content": "a"}]
        self.assertEqual(llm_agent._trim_history(hist, limit=3), [])
        self.assertEqual(llm_agent._trim_history(hist, limit=4)[0]["role"], "user")

    def test_failed_turn_does_not_corrupt_history(self):
        import openai
        a, client = agent([reply("Привет!")])
        a.handle("привет")
        before = list(a.history)
        a._client = RaisingClient(_http_error(openai.RateLimitError, 429))
        with self.assertRaises(llm_agent.LLMUnavailable):
            a.handle("ещё")
        self.assertEqual(a.history, before)


class TestVertexConfig(unittest.TestCase):
    def test_vertex_without_service_account_is_unavailable(self):
        with mock.patch.object(llm_agent, "VERTEX_SA_FILE", Path("/нет/такого.json")):
            import openai
            with self.assertRaises(llm_agent.LLMUnavailable):
                llm_agent._vertex_client(llm_agent.VERTEX, openai)

    def test_broken_service_account_is_unavailable(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / "sa.json").write_text('{"type": "service_account"}', encoding="utf-8")
            with mock.patch.object(llm_agent, "VERTEX_SA_FILE", d / "sa.json"):
                import openai
                with self.assertRaises(llm_agent.LLMUnavailable):
                    llm_agent._vertex_client(llm_agent.VERTEX, openai)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_provider_has_url_and_model(self):
        self.assertTrue(llm_agent.VERTEX.base_url.startswith("https://"))
        self.assertTrue(llm_agent.VERTEX.model)


class TestAssistantTrace(unittest.TestCase):
    def test_unavailable_llm_is_reported_in_trace(self):
        class NoLLM:
            provider = llm_agent.Provider("x", "http://x", "m")
            executor = llm_agent.ToolExecutor(dry_run=True)

            def handle(self, phrase, hint=""):
                raise llm_agent.LLMUnavailable("нет сети")

        def parse(phrase):
            return {"phrase": phrase, "route": "llm", "action": "none", "confidence": 0.9, "fallback": False, "reason": ""}
        res, plan, msg = assistant.dispatch_phrase("вопрос", parser=parse, dry_run=True, agent=NoLLM())
        self.assertIn("недоступна", msg)
        self.assertIn("нет сети", assistant.describe_trace(res))

    def test_local_folder_beats_website(self):
        def parse(phrase):
            return {"phrase": phrase, "route": "browser", "action": "web_open", "site": "education", "query": "", "text": "",
                    "lang": "none", "confidence": 0.95, "fallback": False, "reason": ""}
        with mock.patch.object(assistant.apps, "find_local_item", return_value=Path("/home/u/Desktop/education")), \
                mock.patch.object(assistant.apps, "make_app_plan", return_value=None):
            res, _, _ = assistant.dispatch_phrase("открой education", parser=parse, dry_run=True)
        self.assertEqual(res["route"], "app")
        self.assertIn("локальная", assistant.describe_trace(res))

    def test_timings_are_recorded(self):
        def parse(phrase):
            return {"phrase": phrase, "route": "system", "action": "sys_time", "site": "", "query": "", "text": "",
                    "confidence": 0.99, "fallback": False, "reason": ""}
        res, _, _ = assistant.dispatch_phrase("который час", parser=parse, dry_run=True)
        self.assertGreaterEqual(res["trace"]["parse_s"], 0)
        self.assertGreaterEqual(res["trace"]["exec_s"], 0)


class TestToolRobustness(unittest.TestCase):
    def test_action_called_as_tool_name_is_mapped_and_still_confirmed(self):
        asked = []
        ex = llm_agent.ToolExecutor(dry_run=False, confirm=lambda q: asked.append(q) or False)
        out = ex.run("fs_rename", {"query": "education", "site": "учёба"})
        self.assertEqual(out, "Пользователь отказался, действие не выполнено.")
        self.assertTrue(asked)
        self.assertTrue(ex.log[-1].startswith("system_action("))

    def test_failed_call_is_logged(self):
        ex = llm_agent.ToolExecutor(dry_run=True, confirm=lambda q: True)
        with self.assertRaises(Exception):
            ex.run("no_such_tool", {})
        self.assertIn("ОШИБКА", ex.log[-1])


if __name__ == "__main__":
    unittest.main(verbosity=1)
