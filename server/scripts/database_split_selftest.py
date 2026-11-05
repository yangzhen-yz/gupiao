"""Regression guard for the database.py split.

Ensures the DAL split preserved every public function: each name that existed in the
monolithic database.py must still be importable (and callable) from server.db.database.

Run: .venv/Scripts/python.exe server/scripts/database_split_selftest.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db.database as D

EXPECTED = ['_bump_daily_recs_write_version', '_db_cache_get', '_db_cache_invalidate', '_db_cache_set', '_ensure_optimizer_v2_columns', '_finish_ts_migration', '_migrate_sim_fk', '_payload_hash', '_prepare_ts_migration', '_row_to_recommendation', 'add_stock_basic_info', 'add_stock_concept_tags', 'add_user_realized_pnl', 'daily_recs_write_version', 'delete_daily_recommendations_by_date', 'delete_user_position', 'get_db_conn', 'get_user_capital', 'get_user_realized_pnl', 'init_db', 'list_user_ids', 'load_api_usage_history', 'load_api_usage_limits', 'load_api_usage_today', 'load_cls_pool_daily', 'load_cross_check', 'load_daily_market_reviews', 'load_daily_recommendations', 'load_fund_flow_snapshot', 'load_fundamental_batch', 'load_holder_num_batch', 'load_hot_search_ranking', 'load_hot_stock_buttons', 'load_kline_cache', 'load_latest_focus_plan', 'load_latest_trend_backtest_report', 'load_optimization_history', 'load_position_trade_log', 'load_recommendation_batches', 'load_recommendation_group', 'load_stock_alias_map', 'load_stock_basic_info', 'load_stock_concept_tags', 'load_strategy_factor_weights', 'load_theme_chain_daily', 'load_theme_member_daily', 'load_trend_backtest_records', 'load_trend_backtest_records_pending_verification', 'load_trend_backtest_reports', 'load_trend_dimension_weights', 'load_trend_scan_freshness', 'load_trend_scan_results', 'load_user_positions', 'load_user_scan_pool', 'load_valuation_batch', 'load_westock_snapshot', 'remove_stock_tag', 'save_api_usage_daily', 'save_api_usage_limit', 'save_cls_plate_daily', 'save_cls_pool_daily', 'save_cross_check', 'save_daily_market_reviews', 'save_daily_recommendations', 'save_fund_flow_snapshot', 'save_fundamental_batch', 'save_holder_num_batch', 'save_hot_search_ranking', 'save_hot_stock_buttons', 'save_kline_cache', 'save_optimization_history', 'save_position_trade_log', 'save_predictions', 'save_stock_basic_info', 'save_stock_concept_tags', 'save_stock_industry', 'save_strategy_factor_weights', 'save_theme_chain_daily', 'save_theme_member_daily', 'save_trend_backtest_records', 'save_trend_dimension_weights', 'save_trend_scan_results', 'save_user_capital', 'save_user_position', 'save_user_scan_pool', 'save_valuation_batch', 'save_westock_snapshot', 'update_trend_backtest_verification', 'upsert_trend_backtest_report']


def main():
    missing = [n for n in EXPECTED if not (hasattr(D, n) and callable(getattr(D, n)))]
    if missing:
        print("FAIL: missing/callable mismatch:", missing)
        return 1
    # real-DB smoke: open a connection and read a table-backed function
    try:
        conn = D.get_db_conn()
        conn.close()
        sample = D.load_stock_basic_info()
        assert isinstance(sample, dict), type(sample)
    except Exception as e:  # pragma: no cover
        print("FAIL: real-DB smoke error:", repr(e)[:200])
        return 1
    print(f"OK: all {len(EXPECTED)} DAL functions preserved + DB smoke passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
