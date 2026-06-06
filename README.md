# iot-device-registry-server

Flask-based device registration and management server for IoT devices. It provides:

- Automatic device registration API for ESP32 + 4G modules
- Web admin pages for approving and assigning devices
- Device configuration API for MQTT connection parameters
- MongoDB persistence for registration state and device metadata

This document focuses on the device-side protocol, so it can be implemented directly on ESP32 firmware.

## 1. Device-side overall flow

For an ESP32 device with a 4G module, the expected workflow is:

1. Power on and bring up the cellular data connection.
2. Send an HTTP `POST` request to the server registration endpoint.
3. Parse the JSON response.
4. The request must include `chip_id`, `product_type` (device class), and `device_address`.
5. If the class and address are valid and the address slot is configured, the server returns full MQTT parameters immediately.
6. Optionally call the config endpoint again after reboot or reconnect to refresh configuration.
7. Use the returned MQTT broker, username, password, telemetry topic, and fixed sensor assignment to upload telemetry.

## 2. Important distinction: HTTP server address vs MQTT broker address

These are two different addresses:

- HTTP registration/config server address: your deployed Flask service address, for example `http://<server-ip>:8080`
- MQTT broker address: returned by the server in JSON, default is `47.104.248.242`

Do not send registration packets to the MQTT broker IP. Registration is done over HTTP.

## 3. HTTP server base address

The Flask app listens on:

- Host: `0.0.0.0`
- Port: `8080`

So for a device on the network, the actual base URL is typically:

```text
http://<your-server-ip>:8080
```

Example:

```text
http://192.168.1.100:8080
```

Or if deployed on a public server:

```text
http://example.com:8080
```

## 4. Registration API

### 4.1 Request address

```text
POST /api/register
```

Full example:

```text
http://<your-server-ip>:8080/api/register
```

### 4.2 Request method

```text
POST
```

### 4.3 Required HTTP header

```http
Content-Type: application/json
```

### 4.4 Request body format

The server only accepts JSON. Form data and plain text are not accepted.

Request body schema:

```json
{
	"chip_id": "string, required",
	"product_type": "string, required",
	"device_address": "integer, required"
}
```

Field meanings:

- `chip_id`: unique hardware identifier of the device, required
- `product_type`: device class / model type, required, and must exist in the backend "子传感器分组地址表" (`device_subsensor_groups.device_class`)
- `device_address`: the actual subdevice address used to locate the matching range and slot in the address table

### 4.5 Recommended `chip_id` rules for ESP32

`chip_id` must be stable and globally unique per device. Recommended sources:

- ESP32 MAC address
- ESP32 eFuse-derived chip identifier
- A serial number burned during manufacturing

Recommended formatting:

- ASCII only
- no spaces
- fixed and never changes after factory flashing

Example values:

```text
ESP32_A1B2C3D4E5F6
24D7EB12AB34
soilnode_v1_000123
```

If `chip_id` changes after firmware upgrade, the server will treat it as a new device.

### 4.6 Recommended `product_type` values

Example:

```text
esp32_4g_soil_sensor
esp32_air780e_gateway
greenhouse_node_v2
```

This field is part of registration authentication. Requests with unknown class values are rejected.

### 4.7 Example registration request

```http
POST /api/register HTTP/1.1
Host: 192.168.1.100:8080
Content-Type: application/json

{"chip_id":"24D7EB12AB34","product_type":"esp32_4g_soil_sensor","device_address":101}
```

Equivalent JSON body only:

```json
{
	"chip_id": "24D7EB12AB34",
	"product_type": "esp32_4g_soil_sensor",
	"device_address": 101
}
```

## 5. Registration response details

The registration API authenticates the subdevice against the configured address table and returns MQTT allocation immediately after successful matching.

### 5.1 Successful registration: MQTT config returned immediately

When the server sees a valid `chip_id` + `product_type` + `device_address` combination, and the address maps to a configured slot:

HTTP status:

```text
201 Created
```

JSON body:

```json
{
	"device_id": "dev_ab12cd",
	"device_class": "esp32_4g_soil_sensor",
	"device_addresses": [101, 102, 103],
	"range_id": "6841d4b7a2d0d9097c2d1234",
	"range_start": 100,
	"range_end": 108,
	"slot": 2,
	"mqtt_broker": "47.104.248.242",
	"mqtt_port": 1883,
	"mqtt_username": "dev_ab12cd",
	"mqtt_password": "0123456789abcdef0123456789abcdef",
	"telemetry_topic": "agriculture/device/dev_ab12cd/telemetry",
	"region_id": "",
	"sensor_assignment": {
		"sensor_key": "acme:th300",
		"brand": "acme",
		"sensor_name": "th300",
		"attributes": ["temperature", "humidity"]
	},
	"entities": [
		{
			"entity_no": 34,
			"addresses": [101, 102, 103],
			"sensors": [
				{"address": 101, "sensor_name": "soil4", "metrics": ["soil4_temperature"]}
			]
		}
	]
	}
}
```

Meaning:

- `device_id`: server-assigned logical ID, unique and stable
- `device_class`: the authenticated device class
- `device_addresses`: the incoming address list used for authentication and entity resolution
- `range_id`, `range_start`, `range_end`: matched address segment metadata
- `slot`: calculated slot inside the segment, based on the address and interval
- `mqtt_broker`: MQTT server address to connect to
- `mqtt_port`: MQTT TCP port
- `mqtt_username`: currently the same value as `device_id`
- `mqtt_password`: per-device MQTT password generated by the server
- `telemetry_topic`: MQTT topic used by the device for telemetry uploads
- `region_id`: management-side region identifier
- `sensor_assignment`: the fixed sensor definition and attribute list for the matched slot
- `entities`: grouped observation entities resolved from the address list

### 5.2 Disabled device

If an admin disables the device:

HTTP status:

```text
403 Forbidden
```

JSON body:

```json
{
	"error": "device_disabled"
}
```

Firmware should treat this as a hard stop and avoid infinite rapid retries.

### 5.3 Deleted device

If an admin deletes the device record from the management system, the server removes the MongoDB document entirely.

Effect:

- the next request with the same `chip_id` is treated as a brand new device
- the server generates a new `device_id`
- the device again goes through registration, approval, and configuration from the start

This is different from disabling: disabled devices stay in the database and still return `device_disabled`.

### 5.4 Invalid request

If the body is not JSON:

HTTP status:

```text
400 Bad Request
```

JSON body:

```json
{
	"error": "Request body must be JSON"
}
```

If `chip_id` is empty:

```json
{
	"error": "chip_id is required"
}
```

If `product_type` is empty or invalid:

```json
{
	"error": "invalid_registration_payload"
}
```

If `product_type` is not in the sensor class list (`device_subsensor_groups`):

```text
403 Forbidden
```

```json
{
	"error": "unsupported_device_class"
}
```

If `device_address` is outside all configured ranges for that class:

```text
403 Forbidden
```

```json
{
	"error": "device_address_not_allowed"
}
```

If the matching slot in the address table has not been assigned a sensor yet:

```json
{
	"error": "device_address_unassigned"
}
```

If an existing device sends a different class from its recorded class:

```text
403 Forbidden
```

```json
{
	"error": "device_class_mismatch"
}
```

If the same `chip_id` tries to authenticate with a different configured address:

```json
{
	"error": "device_address_mismatch"
}
```

If the target address is already occupied by another online device:

```text
409 Conflict
```

```json
{
	"error": "device_address_in_use"
}
```

## 6. Configuration API

After a device already knows its `device_id`, it can fetch the latest MQTT and sensor configuration.

### 6.1 Request address

```text
GET /api/devices/<device_id>/config
```

Example:

```text
http://<your-server-ip>:8080/api/devices/dev_ab12cd/config
```

### 6.2 Request method

```text
GET
```

### 6.3 Request headers

No special auth header is required by the current server code.

### 6.4 Success response

If the device exists and is active, the response body is the same structure as the active registration response:

```json
{
	"device_id": "dev_ab12cd",
	"device_class": "esp32_4g_soil_sensor",
	"device_addresses": [101, 102, 103],
	"mqtt_broker": "47.104.248.242",
	"mqtt_port": 1883,
	"mqtt_username": "dev_ab12cd",
	"mqtt_password": "0123456789abcdef0123456789abcdef",
	"telemetry_topic": "agriculture/device/dev_ab12cd/telemetry",
	"region_id": "greenhouse_a",
	"entities": []
}
```

### 6.5 Error responses

Device not found:

```text
404 Not Found
```

```json
{
	"error": "device not found"
}
```

Device exists but is not active:

```text
403 Forbidden
```

```json
{
	"error": "device not active"
}
```

## 7. Data actually stored by the server on first registration

On first registration, the backend creates these values internally:

- `chip_id`: from device request
- `product_type`: from device request
- `device_id`: auto-generated, format `dev_` + 6 lowercase letters/numbers
- `status`: initially `pending`
- `mqtt_password`: random password generated by server
- `mqtt_broker`: default `47.104.248.242`
- `mqtt_port`: default `1883`
- `region_id`: initially empty string
- `device_addresses`: normalized sorted address list reported by the host chip
- `entities`: resolved entity groups and per-sensor metrics derived from address table
- `registered_at`: server timestamp
- `last_seen`: server timestamp
- `assigned_by`: initially empty string
- `assigned_at`: initially `null`

An admin later activates the device and fills in region/sensor configuration on the web page.

## 8. ESP32 firmware implementation advice

The server protocol is simple enough that the ESP32 can implement it in either of these ways:

- ESP32 directly performs HTTP with the 4G link already exposed as a network interface
- ESP32 drives the 4G module with AT commands and asks the module to send HTTP requests

In both cases, the actual application-layer payload sent to the server is the same JSON body described above.

### 8.1 Registration retry strategy

Recommended behavior:

- On boot, register immediately
- If response is `pending_approval`, retry every 1 to 10 minutes
- If HTTP/network failure occurs, use exponential backoff
- If response is `device_disabled`, stop MQTT work and report locally

### 8.2 What to store in NVS/flash

Recommended persistent fields:

- `chip_id`
- last known `device_id`
- last known `mqtt_broker`
- last known `mqtt_port`
- last known `mqtt_username`
- last known `mqtt_password`
- last known `telemetry_topic`
- `region_id`
- `entities`

### 8.3 Recommended boot sequence

1. Read `chip_id` and cached config from NVS.
2. Bring up 4G network.
3. Call `POST /api/register`.
4. If full config is returned, overwrite local cache.
5. If only `pending_approval` is returned, optionally use cached config only if your business logic permits it.
6. If no valid config is available, stay in registration retry mode.
7. If valid config is available, connect to MQTT and start telemetry.

### 8.4 Recommended local state machine

Recommended device-side states:

1. `NET_INIT`: power on modem, SIM detection, APN setup, PDP activation
2. `HTTP_REGISTER`: send `POST /api/register`
3. `PENDING`: got `pending_approval`, wait and retry
4. `CONFIG_READY`: received valid MQTT parameters
5. `MQTT_RUNNING`: connect and publish telemetry
6. `DISABLED`: received `device_disabled`, stop normal work and enter maintenance/error state

This is safer than mixing registration and MQTT logic into one linear function.

### 8.5 Parsing rules the firmware should implement

At minimum, firmware should distinguish these cases from the JSON response:

- Contains `"status":"pending_approval"` -> device is not activated yet
- Contains `"error":"device_disabled"` -> do not continue to MQTT
- Contains `"device_id"` and `"mqtt_broker"` -> active config received
- HTTP status `400/403/404/500` -> treat as server-side error path

For embedded parsers, it is better to key off both HTTP status code and JSON fields.

### 8.6 Timeout recommendations

Suggested starting values for embedded implementation:

- DNS resolve timeout: 10 seconds
- TCP connect timeout: 10 to 20 seconds
- HTTP response timeout: 15 to 30 seconds
- Retry interval after network failure: 30 seconds, then exponential backoff
- Retry interval after `pending_approval`: 5 minutes

Tune these based on your carrier network stability.

### 8.7 If using ESP-IDF native HTTP client

If the ESP32 uses an IP-capable modem integration and can access sockets normally, the logic is:

1. Build JSON string like `{"chip_id":"24D7EB12AB34","product_type":"esp32_4g_soil_sensor"}`
2. Set URL to `http://<server-ip>:8080/api/register`
3. Set method to `POST`
4. Add header `Content-Type: application/json`
5. Write the JSON body
6. Read HTTP status code and response body
7. Parse returned JSON and save config to NVS

## 9. 4G module AT command implementation notes

Different 4G modules use different AT command sets, but the workflow is usually the same.

### 9.1 Generic modem workflow

1. Check modem alive: `AT`
2. Check SIM: `AT+CPIN?`
3. Check signal: `AT+CSQ`
4. Attach packet service: `AT+CGATT=1`
5. Configure APN: module-specific
6. Activate PDP context: module-specific
7. Start HTTP service or TCP stack: module-specific
8. Send HTTP `POST` to `/api/register`
9. Read HTTP status code and response body
10. Parse JSON in ESP32 firmware

### 9.2 Things that must be correct in the modem request

- Method must be `POST`
- Path must be `/api/register`
- Host must be your Flask server host and port
- Header must include `Content-Type: application/json`
- Body must be valid JSON text
- `Content-Length` must match the exact byte length if your module requires manual HTTP framing
- Line endings should be standard HTTP CRLF if sending raw requests

### 9.3 Example raw request bytes

If you are using a transparent TCP mode and manually constructing the HTTP request, the payload should conceptually be:

```text
POST /api/register HTTP/1.1\r\n
Host: 192.168.1.100:8080\r\n
Content-Type: application/json\r\n
Content-Length: 67\r\n
\r\n
{"chip_id":"24D7EB12AB34","product_type":"esp32_4g_soil_sensor"}
```

The blank line between headers and body is mandatory.

### 9.4 Recommended ESP32 responsibilities when a modem sends HTTP

Even if the modem sends the HTTP request, the ESP32 firmware should still own:

- generation of the `chip_id`
- generation of the JSON body
- parsing returned JSON
- persistence of `device_id` and MQTT config
- retry policy and state machine

The modem should be treated as a transport component, not as the owner of registration logic.

## 10. Raw packet example for 4G module HTTP mode

If your 4G module sends raw HTTP payloads, this is the request format the server expects:

```http
POST /api/register HTTP/1.1
Host: <your-server-ip>:8080
Content-Type: application/json
Content-Length: 67

{"chip_id":"24D7EB12AB34","product_type":"esp32_4g_soil_sensor"}
```

Expected successful pending response example:

```http
HTTP/1.1 201 CREATED
Content-Type: application/json

{"device_id":"dev_ab12cd","status":"pending_approval"}
```

Expected active response example:

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"device_id":"dev_ab12cd","device_class":"esp32_4g_soil_sensor","device_addresses":[101,102,103],"mqtt_broker":"47.104.248.242","mqtt_port":1883,"mqtt_username":"dev_ab12cd","mqtt_password":"0123456789abcdef0123456789abcdef","telemetry_topic":"agriculture/device/dev_ab12cd/telemetry","region_id":"greenhouse_a","entities":[{"entity_no":34,"addresses":[101,102,103],"sensors":[{"address":101,"sensor_name":"soil4","metrics":["soil4_temperature"]}]}]}
```

## 11. Notes and current limitations

- Current API uses plain HTTP, not HTTPS
- Current API has no device authentication signature, token, or HMAC
- Current API does not define telemetry payload format; it only returns the MQTT topic and credentials
- Current API accepts JSON only for registration
- Current API returns UTF-8 JSON responses suitable for embedded parsing

## 12. Server runtime requirements

### 12.1 Python dependencies

Install dependencies from `requirements.txt`.

### 12.2 Environment variables

Current server code uses these environment variables:

- `SECRET_KEY`
- `MONGO_URI`
- `DEFAULT_MQTT_BROKER`
- `DEFAULT_MQTT_PORT`

If not set, defaults are:

- `MONGO_URI = mongodb://localhost:27017/device_registry`
- `DEFAULT_MQTT_BROKER = 47.104.248.242`
- `DEFAULT_MQTT_PORT = 1883`

### 12.3 Startup command

```bash
python app.py
```

The server listens on port `8080`.

## 13. Minimum device-side implementation checklist

To complete the ESP32 + 4G integration, the firmware should at minimum support:

- HTTP POST JSON request
- HTTP GET request
- JSON serialization for `chip_id` and `product_type`
- JSON parsing for registration/config responses
- Local persistent storage for returned config
- MQTT connection using returned credentials
- Retry/backoff handling for registration failures

If needed, this README can be extended further with concrete ESP32 sample code for `esp_http_client` or with a 4G module AT command sequence.
