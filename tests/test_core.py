import pytest

from tradebot.core.config import Settings
from tradebot.core.symbols import to_binance, to_coin, to_pair


@pytest.mark.parametrize("raw", ["BTC", "btc", " BTC/USD ", "BTCUSDT", "btc/usd"])
def test_symbol_forms_normalize_to_coin(raw):
    assert to_coin(raw) == "BTC"
    assert to_pair(raw) == "BTC/USD"
    assert to_binance(raw) == "BTCUSDT"


def test_numeric_prefixed_coins_survive_round_trip():
    assert to_coin("1000CHEEMSUSDT") == "1000CHEEMS"
    assert to_pair("1000CHEEMS") == "1000CHEEMS/USD"


def test_one_fee_schedule_is_shared_by_every_consumer(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("fees:\n  spot_maker: 0.0002\nexecution:\n  dry_run: false\nbacktest:\n  initial_cash: 5000\n")

    settings = Settings.load(path)

    assert settings.execution.fees.spot_maker == 0.0002
    assert settings.backtest.fees.spot_maker == 0.0002
    assert settings.execution.dry_run is False
    assert settings.backtest.initial_cash == 5000


def test_repo_default_config_matches_model_defaults():
    from pathlib import Path

    default_yaml = Path(__file__).resolve().parents[1] / "config" / "default.yaml"
    assert Settings.load(default_yaml) == Settings()


def test_unknown_config_keys_are_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("execution:\n  not_a_setting: 1\n")

    with pytest.raises(ValueError):
        Settings.load(path)
