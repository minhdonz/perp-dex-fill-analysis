# HL maker-analytics feasibility spike — findings

_window: last 14d · HL trade rows scanned: 5,175,889 (0 without a usable users[] pair) · scan 51.4s_


## 1a. Top makers by maker-side notional (from stored `raw`)

| # | maker | maker vol ($) | mkts | maker trades | taker vol ($) | maker/taker |
|--:|---|--:|--:|--:|--:|--:|
| 1 | `0xecb6…2b00` | 799,666,565 | 5 | 83,234 | 7,273,973 | 109.9x |
| 2 | `0x7b7f…dee2` | 781,166,573 | 4 | 86,192 | 142,548,814 | 5.5x |
| 3 | `0xd4bb…2a4c` | 775,796,064 | 1 | 6,102 | 333,196,822 | 2.3x |
| 4 | `0xf910…0a2d` | 737,881,775 | 5 | 92,888 | 3,710,317 | 198.9x |
| 5 | `0x71d0…8c26` | 686,239,750 | 5 | 68,878 | 62,052,573 | 11.1x |
| 6 | `0xf5d8…ad53` | 600,505,214 | 5 | 386,972 | 663,596,364 | 0.9x |
| 7 | `0x3947…f3b1` | 463,457,063 | 4 | 11,206 | 1,012,449 | 457.8x |
| 8 | `0x34fb…93de` | 422,011,368 | 5 | 12,615 | 5,381,235 | 78.4x |
| 9 | `0xd906…ec1c` | 338,094,097 | 1 | 7,956 | 97,784,101 | 3.5x |
| 10 | `0x5323…b23c` | 337,154,144 | 3 | 42,038 | 193,211,678 | 1.7x |
| 11 | `0xaa7a…af37` | 322,098,900 | 3 | 2,054 | 1,012,449 | 318.1x |
| 12 | `0xb8eb…1dd5` | 304,697,774 | 1 | 1,882 | 91,907,162 | 3.3x |
| 13 | `0x621c…63ab` | 259,434,673 | 4 | 8,785 | 1,012,449 | 256.2x |
| 14 | `0xd911…961e` | 235,070,402 | 1 | 3,205 | 35,688,007 | 6.6x |
| 15 | `0xc6ac…8e84` | 232,181,546 | 1 | 6,202 | 55,106,910 | 4.2x |
| 16 | `0x8f63…dd74` | 231,349,497 | 3 | 11,052 | 5,555,295 | 41.6x |
| 17 | `0x023a…2355` | 230,491,829 | 5 | 56,577 | 12,887,716 | 17.9x |
| 18 | `0x09bc…410d` | 225,513,688 | 1 | 4,737 | 62,284,164 | 3.6x |
| 19 | `0x0bfd…0a7d` | 215,087,275 | 2 | 16,548 | 67,741,041 | 3.2x |
| 20 | `0x1c1c…bed0` | 193,756,337 | 4 | 121,866 | 0 | ∞ |

_total maker notional in window: $18,809,970,591; top-20 share: 44.6% of it (19605 makers seen)_


## Market-level OI sanity (`metaAndAssetCtxs`)

```json
{
  "BTC": {
    "openInterest": "31323.2384",
    "markPx": "65821.0",
    "dayNtlVlm": "1691204925.868524313",
    "funding": "0.0000125"
  },
  "ETH": {
    "openInterest": "813752.2826000002",
    "markPx": "1795.0",
    "dayNtlVlm": "1216355760.6289389133",
    "funding": "0.0000125"
  },
  "SOL": {
    "openInterest": "3866543.6399999992",
    "markPx": "73.689",
    "dayNtlVlm": "160524953.8575900197",
    "funding": "-0.0000068387"
  },
  "HYPE": {
    "openInterest": "20304689.3999999985",
    "markPx": "74.83",
    "dayNtlVlm": "1237952614.5727803707",
    "funding": "0.0000125"
  },
  "ZEC": {
    "openInterest": "521906.98",
    "markPx": "511.19",
    "dayNtlVlm": "170885995.9889999628",
    "funding": "0.0000299705"
  }
}
```

## 1b. Per-address endpoint probe (top makers)


### `0xecb6…2b00`  (full: `0xecb63caa47c7c4e77f60f1ce858cf28dc2b82b00`)
```json
{
  "clearinghouseState": {
    "n_positions": 83,
    "open_interest_usd": 114445078,
    "unrealized_pnl_usd": -1609171.55,
    "account_value_usd": 40260618,
    "total_ntl_pos_usd": 114445078,
    "sample_coins": [
      "BTC",
      "ETH",
      "ATOM",
      "SOL",
      "AVAX",
      "BNB"
    ]
  },
  "portfolio": {
    "windows": [
      "day",
      "week",
      "month",
      "allTime",
      "perpDay",
      "perpWeek",
      "perpMonth",
      "perpAllTime"
    ],
    "day_pnlHistory_points": 61,
    "day_pnl_last": [
      1781668416957,
      "-203394.312609"
    ],
    "has_accountValueHistory": true
  },
  "userFunding": {
    "events": 500,
    "net_usdc_window": 23819.3,
    "sample": {
      "time": 1780531200000,
      "hash": "0x0000000000000000000000000000000000000000000000000000000000000000",
      "delta": {
        "type": "funding",
        "coin": "0G",
        "usdc": "-0.00031",
        "szi": "3.0",
        "fundingRate": "0.0000125",
        "nSamples": 24
      }
    }
  },
  "userFees": {
    "userCrossRate": "0.000182",
    "userAddRate": "-0.00003",
    "has_dailyUserVlm": true
  }
}
```

### `0x7b7f…dee2`  (full: `0x7b7f72a28fe109fa703eeed7984f2a8a68fedee2`)
```json
{
  "clearinghouseState": {
    "n_positions": 5,
    "open_interest_usd": 1292129,
    "unrealized_pnl_usd": -2455.41,
    "account_value_usd": 2208847,
    "total_ntl_pos_usd": 1292129,
    "sample_coins": [
      "BTC",
      "ETH",
      "SOL",
      "XRP",
      "HYPE"
    ]
  },
  "portfolio": {
    "windows": [
      "day",
      "week",
      "month",
      "allTime",
      "perpDay",
      "perpWeek",
      "perpMonth",
      "perpAllTime"
    ],
    "day_pnlHistory_points": 13,
    "day_pnl_last": [
      1781668419186,
      "56241.796386"
    ],
    "has_accountValueHistory": true
  },
  "userFunding": {
    "events": 500,
    "net_usdc_window": 2738.94,
    "sample": {
      "time": 1780531200000,
      "hash": "0x0000000000000000000000000000000000000000000000000000000000000000",
      "delta": {
        "type": "funding",
        "coin": "BTC",
        "usdc": "-45.067009",
        "szi": "4.1935875",
        "fundingRate": "0.000006989",
        "nSamples": 24
      }
    }
  },
  "userFees": {
    "userCrossRate": "0.000182",
    "userAddRate": "-0.00003",
    "has_dailyUserVlm": true
  }
}
```

### `0xd4bb…2a4c`  (full: `0xd4bb18ef8d1bc1bfadfcc034ca69628b58b42a4c`)
```json
{
  "clearinghouseState": {
    "n_positions": 1,
    "open_interest_usd": 4710057,
    "unrealized_pnl_usd": 2022.29,
    "account_value_usd": 5160618,
    "total_ntl_pos_usd": 4710057,
    "sample_coins": [
      "BTC"
    ]
  },
  "portfolio": {
    "windows": [
      "day",
      "week",
      "month",
      "allTime",
      "perpDay",
      "perpWeek",
      "perpMonth",
      "perpAllTime"
    ],
    "day_pnlHistory_points": 13,
    "day_pnl_last": [
      1781668421365,
      "133843.243481"
    ],
    "has_accountValueHistory": true
  },
  "userFunding": {
    "events": 201,
    "net_usdc_window": 6145.57,
    "sample": {
      "time": 1780531200000,
      "hash": "0x0000000000000000000000000000000000000000000000000000000000000000",
      "delta": {
        "type": "funding",
        "coin": "BTC",
        "usdc": "-216.387947",
        "szi": "56.88292125",
        "fundingRate": "0.0000024485",
        "nSamples": 24
      }
    }
  },
  "userFees": {
    "userCrossRate": "0.000144",
    "userAddRate": "-0.00003",
    "has_dailyUserVlm": true
  }
}
```

### `0xf910…0a2d`  (full: `0xf9109ada2f73c62e9889b45453065f0d99260a2d`)
```json
{
  "clearinghouseState": {
    "n_positions": 30,
    "open_interest_usd": 2629113,
    "unrealized_pnl_usd": -871.4,
    "account_value_usd": 6579592,
    "total_ntl_pos_usd": 2629113,
    "sample_coins": [
      "BTC",
      "ETH",
      "SOL",
      "BNB",
      "OP",
      "LTC"
    ]
  },
  "portfolio": {
    "windows": [
      "day",
      "week",
      "month",
      "allTime",
      "perpDay",
      "perpWeek",
      "perpMonth",
      "perpAllTime"
    ],
    "day_pnlHistory_points": 13,
    "day_pnl_last": [
      1781668422973,
      "15706.267089"
    ],
    "has_accountValueHistory": true
  },
  "userFunding": {
    "events": 500,
    "net_usdc_window": 1643.75,
    "sample": {
      "time": 1780531200000,
      "hash": "0x0000000000000000000000000000000000000000000000000000000000000000",
      "delta": {
        "type": "funding",
        "coin": "AAVE",
        "usdc": "4.002363",
        "szi": "-184.5854166667",
        "fundingRate": "0.0000125",
        "nSamples": 24
      }
    }
  },
  "userFees": {
    "userCrossRate": "0.000208",
    "userAddRate": "-0.00003",
    "has_dailyUserVlm": true
  }
}
```

### `0x71d0…8c26`  (full: `0x71d0e11ebb6150cebe20cf62f798be1a50108c26`)
```json
{
  "clearinghouseState": {
    "n_positions": 68,
    "open_interest_usd": 8795263,
    "unrealized_pnl_usd": -18769.77,
    "account_value_usd": 7119871,
    "total_ntl_pos_usd": 8795263,
    "sample_coins": [
      "BTC",
      "ETH",
      "DYDX",
      "SOL",
      "AVAX",
      "OP"
    ]
  },
  "portfolio": {
    "windows": [
      "day",
      "week",
      "month",
      "allTime",
      "perpDay",
      "perpWeek",
      "perpMonth",
      "perpAllTime"
    ],
    "day_pnlHistory_points": 17,
    "day_pnl_last": [
      1781668424657,
      "37099.982744"
    ],
    "has_accountValueHistory": true
  },
  "userFunding": {
    "events": 500,
    "net_usdc_window": 5105.45,
    "sample": {
      "time": 1780704000000,
      "hash": "0x0000000000000000000000000000000000000000000000000000000000000000",
      "delta": {
        "type": "funding",
        "coin": "2Z",
        "usdc": "-0.000008",
        "szi": "-79.5",
        "fundingRate": "-0.0000007251",
        "nSamples": 2
      }
    }
  },
  "userFees": {
    "userCrossRate": "0.000208",
    "userAddRate": "-0.00002",
    "has_dailyUserVlm": true
  }
}
```

## Endpoint matrix

| endpoint | returns | gives us |
|---|---|---|
| metaAndAssetCtxs | yes | market OI, mark, day vol, funding |
| clearinghouseState | yes | per-maker OI, positions, unreal PnL, acct value |
| portfolio | yes | daily PnL + account-value history |
| userFunding | yes | funding paid/received per account |
| userFees | yes | taker rate + maker rate (rebate) + 14d vol |
