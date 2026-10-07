Venue-shaped Roostoo responses used by `tests/test_venue_failures.py`.

Field names, nesting and the quirks the coordinator must tolerate follow the Roostoo API documentation:
`SpotWallet` balances, `OrderMatched` rows with Role/StopType/Commission* fields, PENDING rows reporting
`FilledQuantity == Quantity` (treated as unfilled), `{"Success": false, "ErrMsg": "no order matched"}`
for an empty query, and `CanceledList` on cancel. Short fields follow `tradebot.exchange.client`.
Values are synthetic. No real account data.
