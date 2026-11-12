import json
import logging
import aiohttp

from datetime import datetime

_LOGGER = logging.getLogger(__name__)
NATURE_REMO_URL = "https://api.nature.global/1"


class NatureRemoAPI:
    """
    Nature RemoのAPIを管理するクラス.
    Class to handle Nature Remo API communication.
    """

    def __init__(self, token) -> None:
        """
        Nature Remo APIの初期化.
        Initialize the Nature Remo API.
        """
        self._token = token

    async def _get(self, path: str):
        """
        Nature RemoのAPI GETリクエスト用の内部メソッド.
        Internal method to perform GET requests to the Nature Remo API.
        """
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"{NATURE_REMO_URL}{path}"
        async with (
            aiohttp.ClientSession() as session,
            session.get(url, headers=headers) as response,
        ):
            if response.status == 429:
                _LOGGER.warning("API制限に達しました! 429 Too Many Requests.")

            # レート制限系のヘッダを取得・ログ出力
            rate_limit = response.headers.get("X-Rate-Limit-Limit")
            rate_remaining = response.headers.get("X-Rate-Limit-Remaining")
            rate_reset = response.headers.get("X-Rate-Limit-Reset")
            # rate_resetを読める時間に変換する
            reset_time = datetime.fromtimestamp(int(rate_reset))

            # デバッグログにリクエスト情報を出力する
            _LOGGER.debug(
                f"NatureRemo RateLimit → Limit: {rate_limit}, Remaining: {rate_remaining}, Reset: {reset_time}"
            )

            if response.status == 200:
                return await response.json()

            _LOGGER.error("Failed to fetch request: %s", response.status)
            return None

    async def get_appliances(self):
        """
        Nature Remo API からすべての家電情報を取得.
        Fetch all appliance information from the Nature Remo API.
        """
        return await self._get("/appliances")

    async def get_devices(self):
        """
        Nature Remoのデバイス一覧を取得（温湿度センサー含む）.
        Retrieve the list of devices from Nature Remo, including temperature and humidity sensors.
        """
        return await self._get("/devices")

    async def send_command_climate(self, payload, appliance_id):
        """
        Nature Remo APIを使ってエアコンを操作.
        Control the air conditioner using the Nature Remo API.
        """
        _LOGGER.info("Setting payload: %s", payload)
        headers = {"Authorization": f"Bearer {self._token}"}
        api_url = f"{NATURE_REMO_URL}/appliances/{appliance_id}/aircon_settings"

        async with (
            aiohttp.ClientSession() as session,
            session.post(api_url, headers=headers, data=payload) as response,
        ):
            # レート制限系のヘッダを取得・ログ出力（ヘッダ欠落時も落ちないようにする）
            rate_limit = response.headers.get("X-Rate-Limit-Limit")
            rate_remaining = response.headers.get("X-Rate-Limit-Remaining")
            rate_reset_raw = response.headers.get("X-Rate-Limit-Reset")
            try:
                rate_reset = datetime.fromtimestamp(int(rate_reset_raw))
            except (TypeError, ValueError):
                rate_reset = None

            _LOGGER.debug(
                f"NatureRemoRateLimit → Limit: {rate_limit}, Remaining: {rate_remaining}, Reset: {rate_reset}"
            )

            text = await response.text()
            if response.status != 200:
                # [custom] 失敗時はエラー本文を状態として扱わせないため None を返す
                _LOGGER.warning(
                    "エアコンの操作に失敗しました / Aircon command failed: status=%s, remaining=%s, reset=%s, payload=%s, body=%s",
                    response.status,
                    rate_remaining,
                    rate_reset,
                    payload,
                    text[:500],
                )
                return None
            try:
                response_json = json.loads(text)
            except ValueError:
                _LOGGER.warning("エアコン操作の応答がJSONではありません: %s", text[:500])
                return None
            _LOGGER.info("エアコンの操作に成功しました: %s", response_json)
            return response_json

    async def send_light_command(self, appliance_id: str, command: str):
        """
        Nature Remo LightのON/OFFを送信.
        Send ON/OFF commands to Nature Remo Light.
        """
        _LOGGER.info(f"Send Light applicance_id:{appliance_id} command:{command}")
        url = f"{NATURE_REMO_URL}/appliances/{appliance_id}/light"

        headers = {"Authorization": f"Bearer {self._token}"}
        payload = {"button": command}

        async with (
            aiohttp.ClientSession() as session,
            session.post(url, headers=headers, data=payload) as response,
        ):
            # レート制限系のヘッダを取得・ログ出力！
            rate_limit = response.headers.get("X-Rate-Limit-Limit")
            rate_remaining = response.headers.get("X-Rate-Limit-Remaining")
            rate_reset = datetime.fromtimestamp(
                int(response.headers.get("X-Rate-Limit-Reset"))
            )

            # デバッグログにリクエスト情報を出力
            _LOGGER.debug(
                f"NatureRemo RateLimit → Limit: {rate_limit}, Remaining: {rate_remaining}, Reset: {rate_reset}"
            )

            response_json = await response.json()
            if response.status == 200:
                _LOGGER.info("照明の操作に成功しました： %s", response_json)
            else:
                error_text = await response.text()
                _LOGGER.error(
                    f"Nature Remo API Error: {response.status} - {error_text}"
                )
            return response_json

    def parse_smart_meter_properties(self, properties: list[dict]) -> dict:
        """
        Nature Remo E / E Liteのechonetlite_propertiesを元に買電・売電・瞬時電力をパースして返却する.
        Parse echonetlite_properties from Nature Remo E / E Lite to extract buy/sell power and instantaneous power.
        """
        # 値が欠損している場合にも備え、各種初期値を定義
        coefficient = 1
        unit_power = 0
        buy_power_raw = 0
        sold_power_raw = 0
        instant_power = 0

        for prop in properties:
            epc = int(prop.get("epc"))
            val_str = prop.get("val", "0")
            try:
                val = int(val_str)
            except ValueError:
                val = 0

            # epc（Echonet Lite Property）に応じて各値を格納
            if epc == 211:  # 係数
                coefficient = val
            elif epc == 215:  # 有効桁数（今回は使用せず）
                continue
            elif epc == 224:  # 積算買電量
                buy_power_raw = val
            elif epc == 225:  # 単位（指数）
                unit_power = val
            elif epc == 227:  # 積算売電量
                sold_power_raw = val
            elif epc == 231:  # 瞬時電力（W）
                instant_power = val

        # 単位変換テーブルを定義
        unit_table = {
            0x00: 1,
            0x01: 0.1,
            0x02: 0.01,
            0x03: 0.001,
            0x04: 0.0001,
            0x0A: 10,
            0x0B: 100,
            0x0C: 1000,
            0x0D: 10000,
        }

        # 取得した値を変換
        factor = coefficient * unit_table.get(unit_power, 1)
        buy_power = buy_power_raw * factor
        sold_power = sold_power_raw * factor

        # センサー用データとして返却
        return {
            "buy_power": buy_power,
            "sold_power": sold_power,
            "instant_power": instant_power,
        }

    async def send_command_signal(self, signal_id: str) -> None:
        """
        指定されたシグナルIDを使ってNature Remo APIを送信する.
        Send a signal by its ID using the Nature Remo API.
        """
        api_url = f"{NATURE_REMO_URL}/signals/{signal_id}/send"
        headers = {"Authorization": f"Bearer {self._token}"}

        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, headers=headers) as response:
                if response.status != 200:
                    text = await response.text()
                    _LOGGER.error("Failed to send signal %s: %s", signal_id, text)
                    response.raise_for_status()
