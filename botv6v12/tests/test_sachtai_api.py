import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("API_BASE_URL", "http://legacy")
os.environ.setdefault("API_KEY", "legacy")
os.environ.setdefault("TOPUP_API_URL", "http://topup")
os.environ.setdefault("FISHY_API_BASE_URL", "http://fishy")
os.environ.setdefault("FISHY_API_KEY", "fishy_key")
os.environ.setdefault("SACHTAI_API_BASE_URL", "https://bot.sachtai.com")
os.environ.setdefault("SACHTAI_API_KEY", "sachtai_key")

import bot


class SachtaiDealerApiServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fishy_sync_does_not_create_products_or_categories(self):
        catalog_item = {
            "external_product_id": "yt1m",
            "name": "YouTube Premium 1 Tháng",
            "api_price": 25000,
            "api_stock": 12,
            "requires_customer_email": True,
        }
        products = []
        with (
            patch.object(bot.FISHY_API, "get_products", new=AsyncMock(return_value=(True, [catalog_item], ""))),
            patch.object(bot, "load_products", new=AsyncMock(return_value=products)),
            patch.object(bot, "save_products", new=AsyncMock()) as save_products,
            patch.object(bot, "ensure_category", new=AsyncMock()) as ensure_category,
        ):
            ok, err, created, updated = await bot.sync_fishy_products()

        self.assertTrue(ok, err)
        self.assertEqual((created, updated), (0, 0))
        self.assertEqual(products, [])
        save_products.assert_not_awaited()
        ensure_category.assert_not_awaited()

    async def test_sync_does_not_create_products_or_categories(self):
        """Catalog synchronization may refresh configured products, never import a catalog."""
        catalog_item = {
            "external_product_id": "NETFLIX1",
            "name": "Netflix 1 tháng",
            "api_price": 25000,
            "api_stock": 12,
            "requires_customer_email": False,
            "required_fields": [],
            "fulfillment": "instant",
            "warranty_hours": 720,
            "api_purchase_enabled": True,
        }
        products = [{
            "id": "local-product",
            "name": "Sản phẩm tự tạo",
            "type": "local",
            "api_provider": None,
            "external_product_id": "",
        }]
        with (
            patch.object(bot.SACHTAI_API, "get_products", new=AsyncMock(return_value=(True, [catalog_item], ""))),
            patch.object(bot, "load_products", new=AsyncMock(return_value=products)),
            patch.object(bot, "save_products", new=AsyncMock()) as save_products,
            patch.object(bot, "ensure_category", new=AsyncMock()) as ensure_category,
        ):
            ok, err, created, updated = await bot.sync_sachtai_products()

        self.assertTrue(ok, err)
        self.assertEqual((created, updated), (0, 0))
        self.assertEqual(products, [{
            "id": "local-product",
            "name": "Sản phẩm tự tạo",
            "type": "local",
            "api_provider": None,
            "external_product_id": "",
        }])
        save_products.assert_not_awaited()
        ensure_category.assert_not_awaited()

    async def test_get_products_parses_products_object(self):
        sample = {
            "success": True,
            "products": {
                "NETFLIX1": {"name": "Netflix 1 tháng", "price": 25000, "stock": 12, "required_fields": [], "fulfillment": "instant", "warranty_hours": 720, "api_purchase_enabled": True},
                "CHATGPT1": {"name": "ChatGPT Plus", "price": 90000, "stock": 0, "required_fields": []},
            },
        }
        with patch.object(bot, "sachtai_request", new=AsyncMock(return_value=(True, sample, "", 200, "9"))):
            ok, items, err = await bot.SACHTAI_API.get_products()
        self.assertTrue(ok, err)
        self.assertEqual([x["external_product_id"] for x in items], ["NETFLIX1", "CHATGPT1"])
        self.assertEqual(items[0]["api_stock"], 12)
        self.assertEqual(items[1]["api_stock"], 0)

    async def test_get_balance_normalized(self):
        with patch.object(bot, "sachtai_request", new=AsyncMock(return_value=(True, {"success": True, "balance": 3524000}, "", 200, None))):
            ok, data, err = await bot.SACHTAI_API.get_balance()
        self.assertTrue(ok, err)
        self.assertEqual(data, {"success": True, "balance": 3524000, "currency": "VND", "balance_text": "3.524.000 ₫"})

    async def test_instant_purchase_normalized_completed(self):
        body = {"success": True, "product": "NETFLIX1", "items": ["a@b.com|pw"], "qty": 1, "total_charged": 25000, "balance_remaining": 1, "order_code": "DH1"}
        with patch.object(bot, "sachtai_request", new=AsyncMock(return_value=(True, body, "", 200, None))):
            ok, data, err = await bot.SACHTAI_API.purchase("NETFLIX1", 1)
        self.assertTrue(ok, err)
        self.assertEqual(data["external_order_code"], "DH1")
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["items"], ["a@b.com|pw"])

    async def test_capcut_purchase_sends_email_and_reference(self):
        mock = AsyncMock(return_value=(True, {"success": True, "order_code": "DH2", "status": "pending", "emails": [{"email": "u@example.com", "status": "pending"}]}, "", 200, None))
        with patch.object(bot, "sachtai_request", new=mock):
            ok, data, err = await bot.SACHTAI_API.purchase(bot.SACHTAI_CAPCUT_ADD_TEAM_KEY, 1, "u@example.com", "BSS-1")
        self.assertTrue(ok, err)
        payload = mock.call_args.args[2]
        self.assertEqual(payload["emails"], ["u@example.com"])
        self.assertEqual(payload["order_code"], "BSS-1")
        self.assertEqual(data["status"], "processing")

    async def test_order_status_email_states(self):
        body = {"success": True, "emails": [{"email": "u@example.com", "status": "in_team"}]}
        with patch.object(bot, "sachtai_request", new=AsyncMock(return_value=(True, body, "", 200, None))):
            ok, data, err = await bot.SACHTAI_API.get_order_status("DH2")
        self.assertTrue(ok, err)
        self.assertEqual(data["status"], "completed")


if __name__ == "__main__":
    unittest.main()
