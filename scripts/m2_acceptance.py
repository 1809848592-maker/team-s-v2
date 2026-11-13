"""M2 acceptance: isolated UI registration, MCP operations and human confirmation."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import subprocess
import sys
import tomllib
import traceback
from datetime import datetime
from pathlib import Path

from m1_acceptance import Acceptance, submission_snapshot, write_json
from playwright.sync_api import expect, sync_playwright


class M2Acceptance(Acceptance):
    def __init__(self, *args, mcp_python: str, claude: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.mcp_python = mcp_python
        self.claude = claude
        self.expected_failures: list[str] = []

    def failure_once(self, page, path, message, action):
        self.expected_failures.append(path)
        self.injecting_logout_failure = True
        page.route(
            "**" + path,
            lambda route: route.fulfill(status=500, json={"detail": message}),
        )
        action()
        expect(page.get_by_role("alert").filter(has_text=message)).to_be_visible()
        page.unroute("**" + path)
        self.injecting_logout_failure = False

    def prepare_forbidden_objects(self):
        page = self.tab("C", "任务与交付", reload=True)
        module_c = self.plan["content"]["modules"][1]["id"]
        page.get_by_label("当前任务", exact=True).select_option(module_c)
        source = self.files / "c-unadopted.md"
        source.write_text("# C 尚未采用的交付\nB 智能体不得读取。\n", encoding="utf-8")
        page.get_by_label("交付文件", exact=True).set_input_files(source)
        page.get_by_label("AI 参与声明", exact=True).select_option("none")
        self.forbidden = self.ui_post(
            page,
            self.path + f"/modules/{module_c}/deliveries",
            page.get_by_role("button", name="提交正式交付", exact=True).click,
        )
        page.get_by_label("新建项目", exact=True).fill("隔离项目-" + self.run_id)
        self.foreign = self.ui_post(
            page,
            "/api/projects",
            page.get_by_role("button", name="创建项目", exact=True).click,
        )
        page.get_by_label("切换项目", exact=True).select_option(self.project["id"])
        page.wait_for_load_state("networkidle")
        self.checked(
            "UI creates C's unadopted delivery and a separate project for real boundary checks"
        )

    def mcp(self, *, revoked=False):
        output = self.output / ("mcp-revoked.json" if revoked else "mcp-report.json")
        command = [
            self.mcp_python,
            str(Path(__file__).with_name("m2_mcp_acceptance.py")),
            "--url",
            self.base_url + "/mcp",
            "--token-file",
            str(self.token_file),
            "--output",
            str(output),
        ]
        if revoked:
            command.append("--expect-revoked")
        else:
            source = self.files / "mcp-draft.md"
            source.write_text(
                "# MCP 智能体交付稿\n术语与来源由本人核查后提交。\n", encoding="utf-8"
            )
            command += [
                "--module-id",
                self.module_b,
                "--forbidden-delivery-id",
                self.forbidden["id"],
                "--foreign-project-id",
                self.foreign["id"],
                "--draft-file",
                str(source),
            ]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=60, check=False
        )
        (self.output / ("mcp-revoked.log" if revoked else "mcp.log")).write_text(
            result.stdout + result.stderr, encoding="utf-8"
        )
        assert result.returncode == 0, (
            "MCP acceptance failed; inspect the local MCP report"
        )
        report = json.loads(output.read_text())
        if not revoked:
            self.draft_id = report["draft_id"]
            self.draft_file = self.files / "mcp-draft.md"
        self.checked(
            "Official MCP client observes revoked token HTTP 401"
            if revoked
            else "Official MCP client reads tasks/spec, reports progress, uploads private draft and rejects forbidden calls"
        )

    def claude_code(self):
        assert self.claude, "Full acceptance requires the real Claude Code executable"
        self.draft_file = self.files / "claude-code-draft.md"
        self.draft_file.write_text(
            "# Claude Code 交付稿\n本人核对后才能正式提交。\n", encoding="utf-8"
        )
        report_path = self.output / "claude-report.json"
        command = [
            self.mcp_python,
            str(Path(__file__).with_name("m2_claude_acceptance.py")),
            "--claude",
            self.claude,
            "--url",
            self.base_url + "/mcp",
            "--token-file",
            str(self.token_file),
            "--mcp-config",
            str(self.output / "claude-mcp.json"),
            "--module-id",
            self.module_b,
            "--draft-file",
            str(self.draft_file),
            "--output",
            str(report_path),
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=150, check=False
        )
        (self.output / "claude.log").write_text(
            result.stdout + result.stderr, encoding="utf-8"
        )
        assert result.returncode == 0, (
            "Real Claude Code MCP verification failed; inspect claude-report.json"
        )
        report = json.loads(report_path.read_text())
        assert report["status"] == "passed" and report["paid_model_calls"] == 0
        self.draft_id = report["draft_id"]
        self.checked(
            "Real Claude Code uses the copied UI config to read tasks/spec, report progress and upload via MCP; model responses stay local"
        )

    def register_agent(self):
        # The exact controls are verified from the rendered page before selecting them.
        page = self.tab("B", "我的智能体", reload=True)
        page.screenshot(path=str(self.output / "agents-empty.png"), full_page=True)
        page.get_by_label("智能体名称", exact=True).fill("M2接入验收助手")
        page.get_by_label("客户端类型", exact=True).select_option("Claude Code")
        self.registered = self.ui_post(
            page,
            self.path + "/agents",
            page.get_by_role("button", name="登记智能体", exact=True).click,
        )
        token = self.registered["token"]
        self.token_file = self.output / "agent-token.json"
        write_json(self.token_file, {"token": token})
        self.token_file.chmod(0o600)
        token_input = page.get_by_label("令牌（仅本次显示）", exact=True)
        assert token_input.input_value() == token, "one-time token differs"
        agents = self.api("B", self.path + "/agents")
        assert (
            len(agents) == 1
            and "token" not in agents[0]
            and "token_hash" not in agents[0]
        )
        assert self.api("A", self.path + "/agents") == []
        assert self.api("C", self.path + "/agents") == []
        page.context.grant_permissions(
            ["clipboard-read", "clipboard-write"], origin=self.base_url
        )
        page.get_by_role("button", name="复制令牌", exact=True).click()
        assert page.evaluate("navigator.clipboard.readText()") == token
        for client, key in (
            ("Claude Code", "claude-code"),
            ("Codex", "codex"),
            ("Cursor", "cursor"),
        ):
            snippet = page.get_by_label(client + " 配置片段", exact=True)
            if not snippet.is_visible():
                page.locator(f"details:has(#mcp-snippet-{key}) > summary").click()
            text = snippet.input_value()
            assert token not in text
            config = tomllib.loads(text) if client == "Codex" else json.loads(text)
            entry = next(
                iter(
                    config[
                        "mcp_servers" if client == "Codex" else "mcpServers"
                    ].values()
                )
            )
            assert entry["url"] == self.base_url + "/mcp"
            if client == "Codex":
                assert entry["bearer_token_env_var"] == "TEAMS_AGENT_TOKEN"
            else:
                assert entry["headers"]["Authorization"] == (
                    "Bearer ${TEAMS_AGENT_TOKEN}"
                    if client == "Claude Code"
                    else "Bearer ${env:TEAMS_AGENT_TOKEN}"
                )
                if client == "Claude Code":
                    assert entry["type"] == "http"
                    write_json(self.output / "claude-mcp.json", config)
            page.get_by_role(
                "button", name="复制 " + client + " 配置", exact=True
            ).click()
            assert page.evaluate("navigator.clipboard.readText()") == text
        assert token not in page.evaluate(
            "JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})"
        )
        page.screenshot(
            path=str(self.output / "agents-configs.png"),
            full_page=True,
            mask=[token_input],
        )
        self.tab("B", "项目看板")
        page = self.tab("B", "我的智能体")
        expect(page.get_by_label("令牌（仅本次显示）", exact=True)).to_have_count(0)
        page.reload(wait_until="networkidle")
        self.tab("B", "我的智能体")
        expect(page.get_by_label("令牌（仅本次显示）", exact=True)).to_have_count(0)
        self.checked(
            "B registers through UI; token is shown once and other members have no management access"
        )
        self.checked(
            "All three client snippets parse, target the real MCP endpoint, copy correctly and contain no token"
        )

    def revoke_and_recover(self):
        page = self.tab("B", "我的智能体")
        self.failure_once(
            page,
            self.path + "/agents",
            "验收注入：智能体列表暂时不可用",
            page.get_by_role("button", name="刷新智能体", exact=True).click,
        )
        page.get_by_role("button", name="重试读取智能体", exact=True).click()
        card = page.locator(
            f'article[data-agent-id="{self.registered["agent"]["id"]}"]'
        )
        expect(card).to_be_visible()
        card.get_by_role("button", name="吊销令牌", exact=True).click()
        revoke_path = self.path + f"/agents/{self.registered['agent']['id']}/revoke"
        self.failure_once(
            page,
            revoke_path,
            "验收注入：吊销暂时失败",
            card.get_by_role("button", name="确认吊销", exact=True).click,
        )
        assert self.api("B", self.path + "/agents")[0]["revoked_at"] is None
        revoked = self.ui_post(
            page,
            revoke_path,
            card.get_by_role("button", name="确认吊销", exact=True).click,
        )
        assert revoked["revoked_at"] is not None
        expect(card.get_by_role("button", name="吊销令牌", exact=True)).to_have_count(0)
        self.mcp(revoked=True)
        token = json.loads(self.token_file.read_text())["token"]
        response = page.context.request.get(
            self.base_url + "/api/agent/me",
            headers={"Authorization": "Bearer " + token},
        )
        assert response.status == 401
        page.screenshot(path=str(self.output / "agents-revoked.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(500)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        page.screenshot(path=str(self.output / "agents-mobile.png"), full_page=True)
        self.checked(
            "Agent list and revoke failures recover; UI revocation immediately invalidates HTTP and MCP; narrow layout fits"
        )

    def confirm_and_audit(self):
        token = json.loads(self.token_file.read_text())["token"]
        auth = {"Authorization": "Bearer " + token, "X-CSRF-Protection": "1"}
        person = self.pages["B"].context.request
        denied_confirm = person.post(
            self.path + f"/drafts/{self.draft_id}/confirm",
            data={"ai_participation": "none"},
            headers=auth,
        )
        assert denied_confirm.status == 403
        denied_spec = person.post(
            self.path + "/spec/versions",
            data={"items": self.spec["items"], "change_note": "智能体不能修改规范"},
            headers=auth,
        )
        assert denied_spec.status == 403
        assert [
            submission_snapshot(item) for item in self.api("B", self.path + "/deliveries")
        ] == [submission_snapshot(self.forbidden)]
        assert len(self.api("A", self.path + "/spec/versions")) == 1
        page = self.tab("B", "项目看板", reload=True)
        draft_button = page.get_by_role(
            "button", name="确认交付稿 · " + self.draft_file.name, exact=True
        )
        expect(draft_button).to_be_visible()
        expect(
            page.get_by_text("M2接入验收助手 代表", exact=False).first
        ).to_be_visible()
        for role in "AC":
            assert self.api(role, self.path + "/drafts") == []
        draft_button.click()
        record = page.locator(f'article[data-draft-id="{self.draft_id}"]')
        record.get_by_role("button", name="填写声明并确认", exact=True).click()
        expect(record.get_by_label("AI 参与声明", exact=True)).to_have_value("")
        button = record.get_by_role("button", name="确认提交", exact=True)
        expect(button).to_be_disabled()
        record.get_by_label("AI 参与声明", exact=True).select_option("primary")
        record.get_by_label("AI 参与说明（选填）", exact=True).fill(
            "本人已核对智能体稿件。"
        )
        delivery = self.ui_post(
            page, self.path + f"/drafts/{self.draft_id}/confirm", button.click
        )
        self.deliveries = [delivery]
        assert delivery["ai_participation"] == "primary"
        assert delivery["ai_note"] == "本人已核对智能体稿件。"
        assert (
            delivery["sha256"]
            == hashlib.sha256(self.draft_file.read_bytes()).hexdigest()
        )
        for role in "AC":
            assert submission_snapshot(delivery) in [
                submission_snapshot(item) for item in self.api(role, self.path + "/deliveries")
            ]
            assert (
                self.pages[role].context.request.get(delivery["download_url"]).body()
                == self.draft_file.read_bytes()
            )
        events = self.api("A", self.path + "/events")
        self.events = events
        agent_id = self.registered["agent"]["id"]
        agent_events = [
            event
            for event in events
            if event["actor_type"] == "agent" and event["actor_id"] == agent_id
        ]
        assert agent_events and all(
            event["represented_user_id"] == self.accounts["B"]["id"]
            for event in agent_events
        )
        denied = [
            event
            for event in agent_events
            if event["event_type"] == "agent_permission_denied"
        ]
        assert len(denied) >= 5
        assert any(
            e["details"].get("object_id") == self.forbidden["id"] for e in denied
        )
        assert any(
            e["details"].get("requested_project_id") == self.foreign["id"]
            and e["details"]["reason"] == "project_scope"
            for e in denied
        )
        assert {"confirm_draft", "publish_spec"}.issubset(
            {e["details"]["action"] for e in denied}
        )
        assert sum(e["details"]["action"] == "mcp_unknown_tool" for e in denied) == 3
        serialized = json.dumps(events)
        token = json.loads(self.token_file.read_text())["token"]
        assert token not in serialized and "token_hash" not in serialized
        confirmation = next(
            event for event in events if event["event_type"] == "agent_draft_confirmed"
        )
        assert (
            confirmation["actor_type"] == "human"
            and confirmation["actor_id"] == self.accounts["B"]["id"]
        )
        assert confirmation["details"]["ai_participation"] == "primary"
        write_json(self.output / "events.json", events)
        page.screenshot(path=str(self.output / "confirmed-desktop.png"), full_page=True)
        page = self.tab("A", "时间线", reload=True)
        rejection = page.locator('[data-event-type="agent_permission_denied"]')
        expect(rejection).to_have_count(len(denied))
        expect(rejection.first).to_contain_text(
            "代表 " + self.accounts["B"]["username"]
        )
        expect(rejection.first).to_contain_text("智能体权限请求被拒绝")
        page.get_by_label("按操作者查看", exact=True).select_option(agent_id)
        expect(page.locator("[data-event-type]")).to_have_count(len(agent_events))
        page.screenshot(path=str(self.output / "agent-timeline.png"), full_page=True)
        self.checked(
            "Human UI confirmation records AI declaration and shares exact bytes; denied MCP calls retain represented identity"
        )

    def finish(self, failure):
        # Keep known injected failures separate; all other console errors remain fatal.
        ignored = [
            item
            for item in self.console
            if item["type"] == "error"
            and item.get("intentional_logout_failure")
            and "500" in item["text"]
            and any(
                path in item["location"].get("url", "")
                for path in self.expected_failures
            )
        ]
        write_json(self.output / "intentional-console-errors.json", ignored)
        self.console = [item for item in self.console if item not in ignored]
        super().finish(failure)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    parser.add_argument("--output-dir", type=Path, default=Path(".verification/M2"))
    parser.add_argument("--browser-executable")
    parser.add_argument(
        "--claude",
        required=True,
        help="Path to the official Claude Code executable; model calls use a localhost fixture",
    )
    parser.add_argument(
        "--mcp-python",
        default=str(Path(__file__).resolve().parents[1] / "backend/.venv/bin/python"),
    )
    args = parser.parse_args()
    output = args.output_dir.resolve() / (
        "run-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    )
    output.mkdir(parents=True, mode=0o700)
    print(f"Artifacts: {output}", flush=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, executable_path=args.browser_executable
        )
        acceptance = M2Acceptance(
            browser,
            args.base_url,
            output,
            mcp_python=args.mcp_python,
            claude=args.claude,
        )
        failure = None
        try:
            acceptance.register()
            acceptance.create_project_and_invite()
            acceptance.publish_plan()
            acceptance.publish_spec_and_templates()
            acceptance.prepare_forbidden_objects()
            acceptance.register_agent()
            acceptance.mcp()
            acceptance.claude_code()
            acceptance.confirm_and_audit()
            acceptance.revoke_and_recover()
        except Exception:  # noqa: BLE001 - retain all failure artifacts
            failure = traceback.format_exc()
            print(failure, file=sys.stderr)
        finally:
            try:
                acceptance.finish(failure)
            except Exception:  # noqa: BLE001 - retain a nonzero diagnostic exit
                failure = failure or traceback.format_exc()
                print(failure, file=sys.stderr)
            browser.close()
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
