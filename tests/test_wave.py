import unittest
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

from brokers import TransactionType

dotenv = ModuleType("dotenv")
dotenv.load_dotenv = lambda: None
sys.modules.setdefault("dotenv", dotenv)
sys.modules.setdefault("yaml", ModuleType("yaml"))
sys.modules.setdefault("mibian", ModuleType("mibian"))

from strategy.wave import WaveStrategy


class WaveStrategyOrderRestorationTests(unittest.TestCase):
    def setUp(self):
        self.strategy = WaveStrategy.__new__(WaveStrategy)
        self.strategy.already_executing_order = 0
        self.strategy.symbol_name = "NFO:NIFTY26OCT25000CE"
        self.strategy.tag = "WAVE_SCRAPER"
        self.strategy.prev_wave_sell_price = 110
        self.strategy.prev_wave_buy_price = 90
        self.strategy.prev_wave_sell_qty = 75
        self.strategy.prev_wave_buy_qty = 75
        self.strategy.orders = {}
        self.strategy.handle_order_update_call_tracker = {}
        self.strategy.handle_order_update_call_tracker_response_dict = {}
        self.strategy.order_tracker = Mock()
        self.strategy.broker = Mock()
        self.strategy._get_symbol_restrictions = Mock(
            return_value=({"CE": {"buy": "yes", "sell": "yes"}}, False)
        )
        self.strategy.handle_order_update = Mock()
        self.strategy.print_current_status = Mock()
        self.strategy._get_position_for_symbol = Mock(return_value=0)

        order_ids = iter(("sell-1", "buy-1"))

        def place_order(**kwargs):
            order_id = next(order_ids)
            self.strategy.handle_order_update_call_tracker_response_dict[order_id] = {
                "order_id": order_id
            }
            return SimpleNamespace(order_id=order_id)

        self.strategy.broker.place_order.side_effect = place_order

    def test_restores_both_missing_orders_as_reciprocal_pair(self):
        self.strategy.check_and_enforce_restrictions_on_active_orders()

        self.assertEqual(
            self.strategy.orders["sell-1"]["associated_order"],
            "buy-1",
        )
        self.assertEqual(
            self.strategy.orders["buy-1"]["associated_order"],
            "sell-1",
        )
        self.assertEqual(
            [
                call.kwargs["transaction_type"]
                for call in self.strategy.broker.place_order.call_args_list
            ],
            [TransactionType.SELL, TransactionType.BUY],
        )

    def test_completed_restored_order_cancels_its_sibling(self):
        self.strategy.check_and_enforce_restrictions_on_active_orders()
        self.strategy.place_wave_order = Mock()

        self.strategy._complete_order("sell-1")

        self.strategy.broker.cancel_order.assert_called_once_with(order_id="buy-1")

    def test_restored_sell_links_to_existing_buy(self):
        self.strategy.orders = {
            "buy-existing": {
                "price": 90,
                "quantity": 75,
                "type": "BUY",
                "associated_order": -1,
            }
        }

        self.strategy.check_and_enforce_restrictions_on_active_orders()

        self.assertEqual(
            self.strategy.orders["sell-1"]["associated_order"],
            "buy-existing",
        )
        self.assertEqual(
            self.strategy.orders["buy-existing"]["associated_order"],
            "sell-1",
        )
        self.strategy.broker.place_order.assert_called_once()

    def test_completion_does_not_cancel_missing_order_sentinel(self):
        self.strategy.orders = {
            "sell-1": {
                "price": 110,
                "associated_order": -1,
            }
        }
        self.strategy.place_wave_order = Mock()

        self.strategy._complete_order("sell-1")

        self.strategy.broker.cancel_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
