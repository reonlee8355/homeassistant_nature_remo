from datetime import timedelta, datetime
import logging

from aiohttp import ClientError

from homeassistant.core import HomeAssistant
from homeassistant.components.light import LightEntity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed


_LOGGER = logging.getLogger(__name__)

# [custom] 残り回数がこれ以下なら定期取得を止め、HAからの操作用に残す
# （エアコン10台を一度に操作しても足りるよう、余裕をもたせている）
RATE_LIMIT_RESERVE = 12


class NatureRemoCoordinator(DataUpdateCoordinator):
    """
    Nature Remo API からデータを取得するコーディネーター.
    Coordinator to fetch data from the Nature Remo API.
    """

    def __init__(self, hass: HomeAssistant, api, update_interval: int = 60) -> None:
        """初期化."""
        super().__init__(
            hass,
            _LOGGER,
            name="Nature Remo Coordinator",
            update_interval=timedelta(seconds=update_interval),
        )
        self.api = api
        self.devices = {}
        self.aircons = {}
        self.lights = {}
        self.ir_remotes = {}
        self.smart_meters = {}
        self.motion_sensors = {}  # motionセンサー用の辞書
        self.entity_map: dict[str, LightEntity] = {}

    async def _async_update_data(self):
        """APIを1回だけ呼び、各アプライアンスの情報を取得."""
        _LOGGER.info("NatureRemoCoordinator.async_update_data start.")
        # [custom] 429後のリセット待ち中、または残り回数が少ないときは
        # APIを呼ばず前回データを維持する（操作用に回数を残す）
        if self.data is not None and (
            self.api.is_rate_limited() or self.api.is_budget_low(RATE_LIMIT_RESERVE)
        ):
            _LOGGER.info(
                "Nature Remo定期取得をスキップ（残り%s回、リセット%s）",
                self.api.rate_limit_remaining,
                self.api.rate_limit_reset,
            )
            return self.data
        try:
            # [custom] 先に2つとも取得し、どちらか失敗したら前回データを維持する
            devices = await self.api.get_devices()
            appliances = await self.api.get_appliances() if devices is not None else None
            if devices is None or appliances is None:
                if self.data is not None:
                    _LOGGER.warning(
                        "Nature Remoのデータ取得に失敗したため前回の状態を維持します"
                    )
                    return self.data
                raise UpdateFailed("Nature Remoのデータ取得に失敗しました")

            # Remoデバイス本体（温湿度センサーなど）の処理
            self.devices = {}
            for device in devices:
                device_id = device.get("id")
                name = device.get("name", "Unnamed")
                newest_events = device.get("newest_events", {})

                # モーションセンサー辞書の追加
                motion_event = newest_events.get("mo")
                if motion_event:
                    created_at_str = motion_event.get("created_at")
                    if created_at_str:
                        # UTCのISO8601文字列をdatetime型に変換して保存しておく
                        created_at = datetime.fromisoformat(
                            created_at_str.replace("Z", "+00:00")
                        )
                        self.motion_sensors[device_id] = {
                            "name": name,
                            "device_id": device_id,
                            "last_motion": created_at,
                            "firmware_version": device.get("firmware_version", ""),
                        }

                # 温湿度センサー辞書の追加
                self.devices[device_id] = {
                    "name": name,
                    "device_id": device_id,
                    "events": newest_events,
                    "firmware_version": device.get("firmware_version", ""),
                }

            # 初期化
            self.aircons = {}
            self.lights = {}
            self.smart_meters = {}
            self.ir_remotes = {}


            for appliance in appliances:
                appliance_type = appliance.get("type")
                appliance_id = appliance.get("id")
                nickname = appliance.get("nickname", "Unnamed")
                device_info = {
                    "name": appliance.get("device", {}).get("name", "No Name"),
                    "device_id": appliance.get("device", {}).get("id", ""),
                    "firmware_version": appliance.get("device", {}).get(
                        "firmware_version", ""
                    ),
                }
                appliance_info = {
                    "name": nickname,
                    "appliance_id": appliance_id,
                    "device": device_info,
                }

                # スマートメーターの処理
                if appliance_type == "EL_SMART_METER":
                    properties = appliance.get("smart_meter", {}).get(
                        "echonetlite_properties", []
                    )
                    parsed = self.api.parse_smart_meter_properties(properties)

                    _LOGGER.debug(
                        f"[{nickname}]buy_power:{parsed["buy_power"]}, sold_power:{parsed["sold_power"]}, current_power:{parsed["instant_power"]}"
                    )
                    self.smart_meters[appliance_id] = {
                        "name": nickname,
                        "appliance_id": appliance_id,
                        "device": device_info,
                        "buy_power": parsed["buy_power"],
                        "sold_power": parsed["sold_power"],
                        "current_power": parsed["instant_power"],
                    }

                # エアコン（AC）の処理
                elif appliance_type == "AC":
                    self.aircons[appliance_id] = appliance_info
                    # signalsにボタンが設定されていればリモートエンティティに追加
                    signals = appliance.get("signals", [])
                    if signals:
                        self.ir_remotes[appliance_id] = {
                            "name": nickname,
                            "appliance_id": appliance_id,
                            "device": device_info,
                            "signals": signals,
                        }

                # 照明（LIGHT）の処理
                elif appliance_type == "LIGHT":
                    self.lights[appliance_id] = appliance_info
                    # signalsにボタンが設定されていればリモートエンティティに追加
                    signals = appliance.get("signals", [])
                    if signals:
                        self.ir_remotes[appliance_id] = {
                            "name": nickname,
                            "appliance_id": appliance_id,
                            "device": device_info,
                            "signals": signals,
                        }

                # IRの処理
                elif appliance_type == "IR":
                    signals = appliance.get("signals", [])
                    if signals:
                        self.ir_remotes[appliance_id] = {
                            "name": nickname,
                            "appliance_id": appliance_id,
                            "device": device_info,
                            "signals": signals,
                        }

            return {ac["id"]: ac for ac in appliances}
        except ClientError as err:
            raise UpdateFailed(f"通信エラー: {err}") from err  # ネットワーク系のエラー
        except TimeoutError as err:
            raise UpdateFailed("APIの応答がタイムアウトしました") from err
        except ValueError as err:
            raise UpdateFailed(f"JSONデータのパースエラー: {err}") from err
