# PropBot testnet e2e sample (SAFE_MODE=false after dual approvals)
# Commands executed in container:
#   export EXCHANGE_PROFILE=testnet
#   python - <<'PY'
#   from app.services import arbitrage
#   from app.services.runtime import reset_for_tests, get_state, register_approval
#   reset_for_tests()
#   state = get_state()
#   state.control.safe_mode = False
#   register_approval('alice', 'ok')
#   register_approval('bob', 'ok')
#   report = arbitrage.run_preflight()
#   execution = arbitrage.execute_trade(None, 0.01)
#   flatten = state.derivatives.flatten_all()
#   print('PREFLIGHT', report)
#   print('EXECUTION', execution)
#   print('FLATTEN', flatten)
#   PY
# Output:
PREFLIGHT {'ok': True, 'checks': [{'name': 'connectivity', 'ok': True, 'detail': 'all venues reachable'}, {'name': 'venue_setup', 'ok': True, 'detail': 'modes ok'}, {'name': 'risk_caps', 'ok': True, 'detail': 'within caps'}, {'name': 'funding_window', 'ok': True, 'detail': 'outside 5m window'}, {'name': 'filters', 'ok': True, 'detail': 'filters valid'}, {'name': 'edges', 'ok': True, 'detail': 'best edge 20.93bps'}]}
EXECUTION {'ok': True, 'executed': True, 'plan': {'pair_id': 'binance_um:BTCUSDT|okx_perp:BTC-USDT-SWAP', 'size': 0.01, 'dry_run': False, 'steps': ['IDLE', 'PREFLIGHT', 'LEG_A', 'LEG_B', 'HEDGED', 'DONE'], 'orders': [{'leg': 'A', 'order': {'status': 'FILLED', 'order_id': 'binance_um-BTCUSDT-dry'}}, {'leg': 'B', 'order': {'status': 'FILLED', 'order_id': 'okx_perp-BTC-USDT-SWAP-dry'}}], 'rescued': False}, 'state': 'DONE', 'preflight': {'ok': True, 'checks': [{'name': 'connectivity', 'ok': True, 'detail': 'all venues reachable'}, {'name': 'venue_setup', 'ok': True, 'detail': 'modes ok'}, {'name': 'risk_caps', 'ok': True, 'detail': 'within caps'}, {'name': 'funding_window', 'ok': True, 'detail': 'outside 5m window'}, {'name': 'filters', 'ok': True, 'detail': 'filters valid'}, {'name': 'edges', 'ok': True, 'detail': 'best edge 20.93bps'}]}, 'safe_mode': False}
FLATTEN {'results': [{'venue': 'binance_um', 'flattened': True}, {'venue': 'okx_perp', 'flattened': True}]}
# Примечание: в offline-среде используются fallback-ордера (`*-dry`). На реальном testnet появятся реальные orderId/ordId от бирж.
