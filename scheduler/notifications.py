"""
Notifications - Alert system for scheduler events.

Sends notifications via:
- Slack webhooks
- Email (SMTP)

Notifications are sent for:
- New high-score companies discovered
- Daily summary reports
- Errors and failures
"""

import logging
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class NotificationConfig:
    """Configuration for notifications."""

    slack_webhook_url: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    alert_email: Optional[str] = None

    @classmethod
    def from_env(cls) -> "NotificationConfig":
        """Load configuration from environment variables."""
        return cls(
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL"),
            smtp_host=os.getenv("SMTP_HOST"),
            smtp_port=int(os.getenv("SMTP_PORT", "587")),
            smtp_user=os.getenv("SMTP_USER"),
            smtp_password=os.getenv("SMTP_PASSWORD"),
            alert_email=os.getenv("ALERT_EMAIL"),
        )


class NotificationService:
    """
    Service for sending notifications about scheduler events.
    """

    def __init__(self, config: Optional[NotificationConfig] = None):
        """
        Initialize notification service.

        Args:
            config: Notification configuration (loads from env if not provided)
        """
        self.config = config or NotificationConfig.from_env()

    def _send_slack(self, message: str, blocks: Optional[List[Dict]] = None) -> bool:
        """Send message to Slack webhook."""
        if not self.config.slack_webhook_url:
            return False

        try:
            payload = {"text": message}
            if blocks:
                payload["blocks"] = blocks

            response = requests.post(
                self.config.slack_webhook_url,
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            return True

        except Exception as e:
            logger.error("Failed to send Slack notification: %s", e)
            return False

    def _send_email(self, subject: str, body: str, html: Optional[str] = None) -> bool:
        """Send email notification."""
        if not all([self.config.smtp_host, self.config.smtp_user, self.config.smtp_password, self.config.alert_email]):
            return False

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.config.smtp_user
            msg["To"] = self.config.alert_email

            msg.attach(MIMEText(body, "plain"))
            if html:
                msg.attach(MIMEText(html, "html"))

            with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port) as server:
                server.starttls()
                server.login(self.config.smtp_user, self.config.smtp_password)
                server.send_message(msg)

            return True

        except Exception as e:
            logger.error("Failed to send email notification: %s", e)
            return False

    def notify_new_company(
        self,
        company_name: str,
        ai_score: int,
        startup_classification: str,
        city: Optional[str] = None,
    ):
        """
        Notify about a newly discovered high-score company.

        Only sends notifications for companies with:
        - AI score >= 3 (significant AI relevance)
        - OR startup classification == 'startup'
        """
        if ai_score < 3 and startup_classification != "startup":
            return  # Don't notify for low-score companies

        location = f" ({city})" if city else ""
        emoji = "🚀" if startup_classification == "startup" else "🔬"

        message = (
            f"{emoji} *New AI/Robotics Company Discovered*\n\n"
            f"*{company_name}*{location}\n"
            f"AI Score: {ai_score} | Classification: {startup_classification}"
        )

        self._send_slack(message)

        # Email for high-priority discoveries
        if ai_score >= 4 or startup_classification == "startup":
            self._send_email(
                subject=f"[Handelsregister] New {startup_classification}: {company_name}",
                body=f"""
New AI/Robotics Company Discovered

Company: {company_name}
Location: {city or "Unknown"}
AI Score: {ai_score}
Classification: {startup_classification}

View in database for more details.
                """,
            )

    def notify_daily_summary(self, stats: Dict[str, Any]):
        """
        Send daily summary notification.

        Args:
            stats: Dictionary with summary statistics
        """
        new_companies = stats.get("new_companies", 0)
        total_companies = stats.get("total_companies", 0)
        backfill_progress = stats.get("backfill_progress", 0)
        requests_used = stats.get("requests_used", 0)

        message = (
            f"📊 *Handelsregister Daily Summary*\n\n"
            f"New companies today: *{new_companies}*\n"
            f"Total in database: *{total_companies:,}*\n"
            f"Backfill progress: *{backfill_progress:.1f}%*\n"
            f"Requests used: *{requests_used}*"
        )

        self._send_slack(message)

    def notify_error(self, error_message: str, job_type: str = "unknown"):
        """
        Send error notification.

        Args:
            error_message: The error message
            job_type: Type of job that failed
        """
        message = (
            f"❌ *Handelsregister Scheduler Error*\n\n"
            f"Job: {job_type}\n"
            f"Error: {error_message}\n"
            f"Time: {datetime.utcnow().isoformat()}"
        )

        self._send_slack(message)

        self._send_email(
            subject=f"[Handelsregister ERROR] {job_type} job failed",
            body=f"""
Scheduler Error

Job Type: {job_type}
Error: {error_message}
Time: {datetime.utcnow().isoformat()}

Please check the logs for more details.
            """,
        )

    def notify_backfill_complete(self, stats: Dict[str, Any]):
        """
        Send notification when backfill is complete.

        Args:
            stats: Backfill completion statistics
        """
        message = (
            f"✅ *Backfill Complete!*\n\n"
            f"Total combinations searched: *{stats.get('total_combinations', 0):,}*\n"
            f"Companies found: *{stats.get('companies_found', 0):,}*\n"
            f"New companies added: *{stats.get('companies_new', 0):,}*"
        )

        self._send_slack(message)

        self._send_email(
            subject="[Handelsregister] Backfill Complete!",
            body=f"""
Handelsregister Backfill Complete

The historical backfill of the Handelsregister database is now complete.

Statistics:
- Total combinations searched: {stats.get("total_combinations", 0):,}
- Companies found: {stats.get("companies_found", 0):,}
- New companies added: {stats.get("companies_new", 0):,}

The scheduler will now continue with regular discovery jobs.
            """,
        )


# Singleton instance
_notification_service: Optional[NotificationService] = None


def get_notification_service() -> NotificationService:
    """Get the notification service singleton."""
    global _notification_service
    if _notification_service is None:
        _notification_service = NotificationService()
    return _notification_service


def notify_new_company(
    company_name: str,
    ai_score: int,
    startup_classification: str,
    city: Optional[str] = None,
):
    """Convenience function to notify about new company."""
    get_notification_service().notify_new_company(company_name, ai_score, startup_classification, city)


def notify_daily_summary(stats: Dict[str, Any]):
    """Convenience function for daily summary."""
    get_notification_service().notify_daily_summary(stats)


def notify_error(error_message: str, job_type: str = "unknown"):
    """Convenience function for error notification."""
    get_notification_service().notify_error(error_message, job_type)
