"""Legacy data paths preserved from the old bot.

These paths intentionally match the old JSON/file layout so existing user,
wallet, product, order, reserve, warranty, and direct-payment data remains
usable after replacing the bot code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LegacyPaths:
    data_dir: Path

    @property
    def product_data_dir(self) -> Path:
        return self.data_dir / "product_data"

    @property
    def reserve_dir(self) -> Path:
        return self.data_dir / "reserve"

    @property
    def orders_data_dir(self) -> Path:
        return self.data_dir / "orders_data"

    @property
    def warranty_dir(self) -> Path:
        return self.data_dir / "warranty"

    @property
    def warranty_archive_dir(self) -> Path:
        return self.data_dir / "warranty_archive"

    @property
    def direct_pay_dir(self) -> Path:
        return self.data_dir / "direct_payments"

    @property
    def topup_control(self) -> Path:
        return self.data_dir / "topup_control.json"

    @property
    def processed_tx(self) -> Path:
        return self.data_dir / "processed_topup_tx.json"

    @property
    def product_details(self) -> Path:
        return self.data_dir / "product_details.json"

    @property
    def bot_instance_lock(self) -> Path:
        return self.data_dir / "bot.instance.lock"

    def ensure(self) -> None:
        for path in (
            self.data_dir,
            self.product_data_dir,
            self.reserve_dir,
            self.orders_data_dir,
            self.warranty_dir,
            self.warranty_archive_dir,
            self.direct_pay_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
