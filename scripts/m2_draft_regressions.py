"""M2-06 browser regression; use only an isolated test database and storage."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import traceback
from datetime import datetime
from pathlib import Path

from m1_acceptance import Acceptance
from playwright.sync_api import expect, sync_playwright


class DraftAcceptance(Acceptance):
    def check_private_confirmation(self):
        page = self.pages["B"]
        headers = {"X-CSRF-Protection": "1"}
        response = page.context.request.post(
            self.path + "/agents",
            data={"name": "M2私稿助手", "client_type": "HTTP regression"},
            headers=headers,
        )
        assert response.status == 201, response.text()
        agent = response.json()
        auth = {"Authorization": "Bearer " + agent["token"]}
        content = "# 仅本人确认的智能体稿\n核查资料后提交。\n".encode()
        response = page.context.request.post(
            f"/api/agent/tasks/{self.module_b}/drafts",
            multipart={
                "file": {
                    "name": "m2-private-draft.md",
                    "mimeType": "text/markdown",
                    "buffer": content,
                },
                "handoff_note": "保留上传时的交接说明。",
            },
            headers=auth,
        )
        assert response.status == 201, response.text()
        draft = response.json()["content"]
        response = page.context.request.post(
            f"/api/agent/tasks/{self.module_b}/progress",
            data={
                "stage": "checking",
                "summary": "智能体已输出草稿，等待本人核查。",
            },
            headers=auth,
        )
        assert response.status == 201, response.text()
        for role in "AC":
            assert self.api(role, self.path + "/drafts") == []
            for suffix in ("", "/download"):
                response = self.pages[role].context.request.get(
                    self.path + f"/drafts/{draft['id']}" + suffix
                )
                assert response.status == 404
        assert self.api("B", self.path + "/deliveries") == []
        page = self.tab("B", "项目看板", reload=True)
        expect(
            page.get_by_role("button", name="确认交付稿 · m2-private-draft.md")
        ).to_be_visible()
        expect(page.get_by_text("M2私稿助手 代表", exact=False).first).to_be_visible()
        page.get_by_role("button", name="确认交付稿 · m2-private-draft.md").click()
        page.wait_for_load_state("networkidle")
        record = page.locator(f'article[data-draft-id="{draft["id"]}"]')
        saved = self.download(
            page,
            record.get_by_role("link", name="m2-private-draft.md", exact=True),
            "draft.md",
        )
        assert saved.read_bytes() == content
        record.get_by_role("button", name="填写声明并确认", exact=True).click()
        declaration = record.get_by_label("AI 参与声明", exact=True)
        expect(declaration).to_have_value("")
        button = record.get_by_role("button", name="确认提交", exact=True)
        expect(button).to_be_disabled()
        declaration.select_option("primary")
        record.get_by_label("AI 参与说明（选填）", exact=True).fill(
            "本人已复核来源与论证。"
        )
        confirm_path = self.path + f"/drafts/{draft['id']}/confirm"

        def fail_confirmation(route):
            route.fulfill(
                status=500,
                content_type="application/json",
                body=json.dumps({"detail": "验收注入：确认暂时失败"}),
            )

        page.route("**" + confirm_path, fail_confirmation)
        # Reuse the harness's intentional-failure flag; retain the exact expected URL below.
        self.injecting_logout_failure = True
        button.click()
        expect(
            page.get_by_role("alert").filter(has_text="验收注入：确认暂时失败")
        ).to_be_visible()
        expect(declaration).to_have_value("primary")
        expect(button).to_be_enabled()
        assert self.api("B", self.path + "/deliveries") == []
        page.unroute("**" + confirm_path, fail_confirmation)
        self.injecting_logout_failure = False
        self.console = [
            item
            for item in self.console
            if not (
                item["intentional_logout_failure"]
                and confirm_path in item["location"].get("url", "")
                and "500" in item["text"]
            )
        ]
        self.checked(
            "Private draft is owner-only; explicit declaration required; failed confirmation is retryable"
        )
        delivery = self.ui_post(page, confirm_path, button.click)
        self.deliveries = [delivery]
        assert delivery["ai_participation"] == "primary"
        assert delivery["ai_note"] == "本人已复核来源与论证。"
        assert delivery["handoff_note"] == "保留上传时的交接说明。"
        assert delivery["sha256"] == hashlib.sha256(content).hexdigest()
        assert delivery["plan_version_id"] == self.plan["id"]
        assert delivery["spec_version_id"] == self.spec["id"]
        expect(page.locator(".private-drafts > .stack .draft-record")).to_have_count(0)
        page.locator(".confirmed-drafts > summary").click()
        expect(record.get_by_text("已确认提交", exact=True)).to_be_visible()
        expect(record.get_by_role("button", name="确认提交", exact=True)).to_have_count(
            0
        )
        expect(page.locator(".task-context .tag")).to_have_text("已提交")
        page.screenshot(path=str(self.output / "draft-confirmed.png"), full_page=True)
        for role in "AC":
            assert self.api(role, self.path + "/deliveries") == [delivery]
            downloaded = self.pages[role].context.request.get(delivery["download_url"])
            assert downloaded.body() == content
        own = self.api("B", self.path + "/drafts")[0]
        assert own["status"] == "confirmed" and own["delivery_id"] == delivery["id"]
        events = self.api("A", self.path + "/events")
        confirmed = [e for e in events if e["event_type"] == "agent_draft_confirmed"]
        assert len(confirmed) == 1
        assert confirmed[0]["actor_type"] == "human"
        assert confirmed[0]["actor_id"] == self.accounts["B"]["id"]
        assert confirmed[0]["details"]["ai_note"] == delivery["ai_note"]
        self.tab("B", "项目看板")
        expect(
            page.get_by_role("button", name="确认交付稿 · m2-private-draft.md")
        ).to_have_count(0)
        self.checked(
            "Retry creates one shared formal version with declaration, original bytes, latest bindings and human event"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    parser.add_argument(
        "--output-dir", type=Path, default=Path(".verification/M2-06-ui")
    )
    parser.add_argument("--browser-executable")
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
        acceptance = DraftAcceptance(browser, args.base_url, output)
        failure = None
        try:
            acceptance.register()
            acceptance.create_project_and_invite()
            acceptance.publish_plan()
            acceptance.publish_spec_and_templates()
            acceptance.check_private_confirmation()
        except Exception:  # noqa: BLE001 - preserve all browser failure artifacts
            failure = traceback.format_exc()
            print(failure, file=sys.stderr)
        finally:
            try:
                acceptance.finish(failure)
            except Exception:  # noqa: BLE001 - preserve a nonzero diagnostic exit
                failure = failure or traceback.format_exc()
                print(failure, file=sys.stderr)
            browser.close()
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
