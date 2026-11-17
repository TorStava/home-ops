#!/usr/bin/env bash
set -euo pipefail

# Usage: ./onvif-set-bitrate.sh <host> <port> <username> <password> <profile_token> <bitrate_kbps>
# Example: ./onvif-set-bitrate.sh 192.168.1.157 10080 admin password PROFILE_000 4096

HOST="${1:?Usage: $0 <host> <port> <user> <pass> <profile_token> <bitrate>}"
PORT="${2:?}"
USER="${3:?}"
PASS="${4:?}"
PROFILE="${5:?}"
BITRATE="${6:?}"

ENDPOINT="http://${HOST}:${PORT}/onvif/media_service"

gen_ws_security() {
    local nonce_raw
    nonce_raw=$(openssl rand 16)
    local nonce_b64
    nonce_b64=$(echo -n "$nonce_raw" | base64)
    local created
    created=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    local digest
    digest=$(printf '%s%s%s' "$nonce_raw" "$created" "$PASS" | openssl dgst -sha1 -binary | base64)

    cat <<HEADER
      <Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
        <UsernameToken>
          <Username>${USER}</Username>
          <Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">${digest}</Password>
          <Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">${nonce_b64}</Nonce>
          <Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">${created}</Created>
        </UsernameToken>
      </Security>
HEADER
}

echo "=== Step 1: Getting current encoder configuration for ${HOST}:${PORT} ==="

GET_BODY=$(cat <<SOAP
<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Header>
$(gen_ws_security)
  </s:Header>
  <s:Body>
    <trt:GetVideoEncoderConfiguration>
      <trt:ConfigurationToken>V_ENC_000</trt:ConfigurationToken>
    </trt:GetVideoEncoderConfiguration>
  </s:Body>
</s:Envelope>
SOAP
)

RESPONSE=$(curl -s --connect-timeout 5 \
    -H "Content-Type: application/soap+xml; charset=utf-8" \
    -d "$GET_BODY" \
    "$ENDPOINT")

echo "$RESPONSE" | python3 -c "
import sys, xml.dom.minidom
try:
    print(xml.dom.minidom.parseString(sys.stdin.read()).toprettyxml(indent='  '))
except:
    print('Failed to parse response')
" 2>/dev/null || echo "$RESPONSE"

echo ""
echo "=== Step 2: Setting main stream bitrate to ${BITRATE} kbps ==="

SET_BODY=$(cat <<SOAP
<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Header>
$(gen_ws_security)
  </s:Header>
  <s:Body>
    <trt:SetVideoEncoderConfiguration>
      <trt:Configuration token="V_ENC_000">
        <tt:Name>V_ENC_000</tt:Name>
        <tt:UseCount>1</tt:UseCount>
        <tt:Encoding>H264</tt:Encoding>
        <tt:Resolution>
          <tt:Width>1920</tt:Width>
          <tt:Height>1080</tt:Height>
        </tt:Resolution>
        <tt:Quality>6</tt:Quality>
        <tt:RateControl>
          <tt:FrameRateLimit>25</tt:FrameRateLimit>
          <tt:EncodingInterval>1</tt:EncodingInterval>
          <tt:BitrateLimit>${BITRATE}</tt:BitrateLimit>
        </tt:RateControl>
        <tt:H264>
          <tt:GovLength>50</tt:GovLength>
          <tt:H264Profile>Main</tt:H264Profile>
        </tt:H264>
        <tt:SessionTimeout>PT60S</tt:SessionTimeout>
      </trt:Configuration>
      <trt:ForcePersistence>true</trt:ForcePersistence>
    </trt:SetVideoEncoderConfiguration>
  </s:Body>
</s:Envelope>
SOAP
)

RESPONSE=$(curl -s --connect-timeout 5 \
    -H "Content-Type: application/soap+xml; charset=utf-8" \
    -d "$SET_BODY" \
    "$ENDPOINT")

echo "$RESPONSE" | python3 -c "
import sys, xml.dom.minidom
try:
    print(xml.dom.minidom.parseString(sys.stdin.read()).toprettyxml(indent='  '))
except:
    print('Failed to parse response')
" 2>/dev/null || echo "$RESPONSE"

echo ""
echo "=== Step 3: Verifying new configuration ==="

VERIFY_BODY=$(cat <<SOAP
<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Header>
$(gen_ws_security)
  </s:Header>
  <s:Body>
    <trt:GetVideoEncoderConfiguration>
      <trt:ConfigurationToken>V_ENC_000</trt:ConfigurationToken>
    </trt:GetVideoEncoderConfiguration>
  </s:Body>
</s:Envelope>
SOAP
)

RESPONSE=$(curl -s --connect-timeout 5 \
    -H "Content-Type: application/soap+xml; charset=utf-8" \
    -d "$VERIFY_BODY" \
    "$ENDPOINT")

echo "$RESPONSE" | python3 -c "
import sys, xml.dom.minidom
try:
    print(xml.dom.minidom.parseString(sys.stdin.read()).toprettyxml(indent='  '))
except:
    print('Failed to parse response')
" 2>/dev/null || echo "$RESPONSE"

echo ""
echo "Done. Changes:"
echo "  - Bitrate: -> ${BITRATE} kbps"
echo "  - Quality: -> 6"
echo "  - FPS: -> 25"
echo "  - GOP: -> 50 (2s at 25fps)"
echo "  - EncodingInterval: -> 1"
