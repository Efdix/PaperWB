"""邮件通知 —— SMTP 发信框架（文献巡视新文献 / 网页更新提醒共用）。

设计:
- 配置存 config.json 的 email_notify 键（见 utils/config.get_email_config）。
  需要用户在邮箱服务商后台生成「授权码」而非登录密码（QQ/163/Gmail 均如此）。
- send_notification_email() 为同步纯函数（QThread 中调用）；UI 层用
  EmailSendWorker 包装。发送失败只返回 (False, 原因)，绝不抛出到 UI。
- 邮箱账号属于明天才能拿到的信息：框架先行，未配置时 is_configured()
  返回 False，所有提醒入口静默跳过（状态栏提示一次）。
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass, field
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

from PySide6.QtCore import QThread, Signal

from ..utils.config import get_email_config
from ..utils.threads import track

SEND_TIMEOUT = 30.0  # 秒


@dataclass
class EmailConfig:
    """邮件发送配置（来自 config.email_notify）。"""

    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    use_ssl: bool = True
    username: str = ""
    password: str = ""
    sender: str = ""             # 空 = 用 username
    recipients: list[str] = field(default_factory=list)

    @staticmethod
    def from_config(cfg: dict | None = None) -> "EmailConfig":
        cfg = cfg if cfg is not None else get_email_config()
        raw_rcpt = str(cfg.get("recipients", "") or "")
        recipients = [r.strip() for r in raw_rcpt.replace("；", ",").replace("，", ",").split(",")
                      if r.strip()]
        try:
            port = int(cfg.get("smtp_port") or 465)
        except (TypeError, ValueError):
            port = 465
        return EmailConfig(
            enabled=bool(cfg.get("enabled")),
            smtp_host=str(cfg.get("smtp_host", "") or "").strip(),
            smtp_port=port,
            use_ssl=bool(cfg.get("use_ssl", True)),
            username=str(cfg.get("username", "") or "").strip(),
            password=str(cfg.get("password", "") or ""),
            sender=str(cfg.get("sender", "") or "").strip(),
            recipients=recipients,
        )

    def is_configured(self) -> bool:
        """是否具备发信条件（总开关 + 主机 + 账号 + 授权码 + 收件人）。"""
        return (self.enabled and bool(self.smtp_host) and bool(self.username)
                and bool(self.password) and bool(self.recipients))


def send_notification_email(cfg: EmailConfig, subject: str, body: str) -> tuple[bool, str]:
    """发送纯文本通知邮件。返回 (ok, message)。

    同步阻塞（请在后台线程调用）；任何异常都转成 (False, 原因)。
    """
    if not cfg.is_configured():
        return False, "邮件通知未配置或未启用"
    sender_addr = cfg.sender or cfg.username
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("PaperWB", "utf-8")), sender_addr))
    msg["To"] = ", ".join(cfg.recipients)
    msg["Date"] = formatdate(localtime=True)

    try:
        if cfg.use_ssl:
            context = ssl.create_default_context()
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                cfg.smtp_host, cfg.smtp_port, timeout=SEND_TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=SEND_TIMEOUT)
        try:
            if not cfg.use_ssl:
                try:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                except smtplib.SMTPNotSupportedError:
                    pass  # 服务器不支持 STARTTLS，按明文继续（内网/本地网关场景）
            server.login(cfg.username, cfg.password)
            server.sendmail(sender_addr, cfg.recipients, msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
        return True, f"已发送至 {', '.join(cfg.recipients)}"
    except smtplib.SMTPAuthenticationError:
        return False, "登录被拒绝：请确认使用邮箱服务商生成的授权码（而非登录密码）"
    except smtplib.SMTPConnectError as e:
        return False, f"无法连接 SMTP 服务器：{e}"
    except (OSError, ssl.SSLError) as e:
        return False, f"网络错误：{e}"
    except Exception as e:  # noqa: BLE001
        return False, f"发送失败：{e}"


def send_async(subject: str, body: str, on_done=None) -> EmailSendWorker:
    """读取当前配置并发送（后台线程）；on_done(ok, msg) 在主线程回调。

    未配置时同样回调 (False, 原因)，由调用方决定是否提示。
    返回 worker（已 track 保活并 start）。
    """
    worker = EmailSendWorker(EmailConfig.from_config(), subject, body)
    if on_done is not None:
        worker.done.connect(on_done)
    track(worker)
    worker.start()
    return worker


class EmailSendWorker(QThread):
    """后台发送邮件（设置页测试按钮 / 巡视与网页提醒共用）。"""

    done = Signal(bool, str)  # (ok, message)

    def __init__(self, cfg: EmailConfig, subject: str, body: str, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._subject = subject
        self._body = body

    def run(self) -> None:
        ok, msg = send_notification_email(self._cfg, self._subject, self._body)
        if not self.isInterruptionRequested():
            self.done.emit(ok, msg)


# ============================================================
# 内容模板
# ============================================================

def format_papers_digest(kind: str, source_label: str,
                         papers: list[dict]) -> tuple[str, str]:
    """把新文献列表格式化为 (subject, body) 纯文本摘要。"""
    subject = f"【PaperWB】{source_label} 发现 {len(papers)} 篇新文献"
    lines = [f"检索入口：{kind}", f"来源：{source_label}",
             f"时间：{formatdate(localtime=True)}", f"新文献：{len(papers)} 篇", ""]
    for i, p in enumerate(papers, 1):
        lines.append(f"{i}. {p.get('title', '') or '（无标题）'}")
        meta = " ".join(filter(None, [
            f"{p.get('authors', '')[:60]}" if p.get("authors") else "",
            f"({p.get('year', '')})" if p.get("year") else "",
            p.get("journal", ""),
        ]))
        if meta:
            lines.append(f"   {meta}")
        ident = p.get("doi") or p.get("pmid") or p.get("arxiv_id") or ""
        if ident:
            lines.append(f"   标识: {ident}")
        if p.get("url"):
            lines.append(f"   链接: {p['url']}")
        abstract = (p.get("abstract") or "").strip()
        if abstract:
            shown = abstract[:300] + ("…" if len(abstract) > 300 else "")
            lines.append(f"   摘要: {shown}")
        lines.append("")
    lines.append("—— 由 PaperWB 检索工作台自动发送")
    return subject, "\n".join(lines)


def format_page_update(page_name: str, url: str, new_items: list[dict],
                       summary: str = "") -> tuple[str, str]:
    """把网页更新（新链接列表）格式化为 (subject, body)。"""
    subject = f"【PaperWB】网页「{page_name}」有新内容"
    lines = [
        f"监控网页：{page_name}",
        f"地址：{url}",
        f"时间：{formatdate(localtime=True)}",
    ]
    if summary:
        lines.append(f"变更摘要：{summary}")
    if new_items:
        lines.append("")
        lines.append(f"新增内容 {len(new_items)} 条：")
        for it in new_items[:20]:
            title = (it.get("title") or "").strip() or "(无标题链接)"
            lines.append(f"· {title}")
            if it.get("url"):
                lines.append(f"  {it['url']}")
        if len(new_items) > 20:
            lines.append(f"…（其余 {len(new_items) - 20} 条略）")
    else:
        lines.append("页面正文内容发生了变化（未识别到新增链接）。")
    lines.append("")
    lines.append("—— 由 PaperWB 网页追踪自动发送")
    return subject, "\n".join(lines)
