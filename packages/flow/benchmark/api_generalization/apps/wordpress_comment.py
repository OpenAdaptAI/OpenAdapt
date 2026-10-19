"""WordPress: a visitor posts a comment on a post (public form, no sign-in)."""

from __future__ import annotations

from typing import Any

from benchmark.api_generalization.apps.wp import WordPressBase, wpsql
from benchmark.api_generalization.common import ExecSqlVerifier, rec_click, rec_text
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr

QUERY = ("SELECT comment_ID AS id, comment_content AS comment, comment_author AS author, "
         "comment_author_email AS email, comment_author_url AS url FROM wp_comments WHERE comment_post_ID = 1")
JUDGE = ("SELECT comment_ID AS id, comment_post_ID, comment_parent, comment_content, comment_author, "
         "comment_author_email, comment_author_url, comment_approved, comment_type, user_id "
         "FROM wp_comments ORDER BY comment_ID")


class App(WordPressBase):
    name = "wordpress_comment"
    workflow = "post-comment"
    form_path = "/?p=1"
    save_path_regex = r"^/wp-comments-post\.php$"
    login_required = False
    unique_param = "comment"
    normal_values = [
        dict(comment="The parking map on page two helped a lot.", author="Ada Park", email="ada.park@example.invalid", url="https://ada.example.invalid/"),
        dict(comment="Could the meeting notes list room numbers?", author="Ravi Osei", email="ravi.osei@example.invalid", url="https://ravi.example.invalid/"),
        dict(comment="Thanks for posting the bus timetable.", author="Lena Fischer", email="lena.f@example.invalid", url="https://lena.example.invalid/"),
    ]
    blank_values = dict(comment="No website for this one.", author="Mo Diallo", email="mo.diallo@example.invalid", url="")
    dup_values = dict(comment="Sam Ortiz", author="Sam Ortiz", email="sam.ortiz@example.invalid", url="https://sam.example.invalid/")
    invalid_values = dict(comment="This email has no at sign.", author="Kim Vu", email="kim.vu.example.invalid", url="")
    invalid_note = "email without @ (the server rejects it)"

    def open_form(self, page: Any) -> None:
        super().open_form(page)
        page.locator("#comment").scroll_into_view_if_needed()  # where a reader starts typing
        page.wait_for_timeout(300)

    def record(self, rec: Any, page: Any, v: dict[str, str]) -> None:
        rec_text(rec, page, "#comment", v["comment"], "comment")
        rec_text(rec, page, "#author", v["author"], "author")
        rec_text(rec, page, "#email", v["email"], "email")
        rec_text(rec, page, "#url", v["url"], "url")
        rec_click(rec, page, "#submit")

    def rows(self) -> list[dict[str, Any]]:
        return wpsql(JUDGE)

    def verifier(self) -> Any:
        return ExecSqlVerifier(wpsql, QUERY, timeout_s=10.0)

    def expect_fields(self, v: dict[str, str]) -> dict[str, str]:
        return {"comment_content": v["comment"], "comment_author": v["author"],
                "comment_author_email": v["email"], "comment_author_url": v["url"],
                "comment_post_ID": "1", "comment_parent": "0"}

    def effects(self) -> list[Effect]:
        match = {"comment": ValueExpr(param="comment")}
        out = [Effect(kind=EffectKind.RECORD_WRITTEN, match=match, expected_count=1, risk="reversible",
                      probe="exactly one comment with this text", timeout_s=8.0)]
        for f in ("author", "email", "url"):
            out.append(Effect(kind=EffectKind.FIELD_EQUALS, match=match, field=f, value=ValueExpr(param=f),
                              risk="reversible", probe=f"comment {f} read-back", timeout_s=8.0))
        return out
