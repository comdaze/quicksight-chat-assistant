"""IAM Identity Center SAML 2.0 (SP-initiated) login and HMAC session tokens for the chat page.

The Identity Center application metadata (XML) is read from the SSM parameter IDP_METADATA_PARAMETER on first use,
so it can be set after deployment (`./qsca idp-metadata <file>`) without redeploying. The signing key is derived from
the Secrets Manager secret SESSION_SECRET_ID (any random string; tools/common/caller.py derives it the same way).

Flow: the chat iframe opens a popup at /auth/login?nonce=..., which redirects to Identity Center with an
AuthnRequest. Identity Center posts a signed assertion to /saml/acs. After validation the popup hands a
signed session token back to the iframe with postMessage, restricted to this origin and matched by nonce.
"""

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
import uuid
import zlib

import boto3
from lxml import etree
from signxml import XMLVerifier
from signxml.exceptions import SignXMLException

NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
    "md": "urn:oasis:names:tc:SAML:2.0:metadata",
}
SP_ENTITY_ID = os.environ["SAML_SP_ENTITY_ID"]
SESSION_SECRET_ID = os.environ["SESSION_SECRET_ID"]
IDP_METADATA_PARAMETER = os.environ["IDP_METADATA_PARAMETER"]
SESSION_SECONDS = int(os.environ.get("SESSION_SECONDS", str(8 * 3600)))
LOGIN_SECONDS = 600
CLOCK_SKEW = dt.timedelta(minutes=3)
SAFE_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, remove_comments=True)


class AuthError(Exception):
    pass


class NotConfigured(Exception):
    """Identity Center metadata has not been stored yet."""


def parse_idp_metadata(xml_bytes):
    root = etree.fromstring(xml_bytes, SAFE_PARSER)
    entity_id = root.get("entityID")
    redirect = root.xpath(
        "//md:IDPSSODescriptor/md:SingleSignOnService[@Binding='urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect']/@Location",
        namespaces=NS,
    )
    certs = root.xpath("//md:IDPSSODescriptor/md:KeyDescriptor[not(@use) or @use='signing']//ds:X509Certificate/text()", namespaces=NS)
    if not (entity_id and redirect and certs):
        raise RuntimeError("Identity Center metadata is missing entityID, redirect binding or signing certificate")
    body = "".join(certs[0].split())
    pem = "-----BEGIN CERTIFICATE-----\n" + "\n".join(body[i:i + 64] for i in range(0, len(body), 64)) + "\n-----END CERTIFICATE-----\n"
    return entity_id, redirect[0], pem


_idp = {}


def idp():
    """(entity id, SSO redirect URL, signing certificate PEM), loaded from SSM and cached for 5 minutes."""
    if not _idp or time.time() - _idp["at"] > 300:
        ssm = boto3.client("ssm")
        try:
            raw = ssm.get_parameter(Name=IDP_METADATA_PARAMETER)["Parameter"]["Value"]
        except ssm.exceptions.ParameterNotFound as error:
            raise NotConfigured("管理员尚未上传 Identity Center 元数据") from error
        _idp.update(at=time.time(), value=parse_idp_metadata(raw.encode("utf-8")))
    return _idp["value"]


_session_key = None


def _key():
    global _session_key
    if _session_key is None:
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=SESSION_SECRET_ID)["SecretString"]
        if len(raw) < 32:
            raise RuntimeError("session key too short")
        _session_key = hashlib.sha256(raw.encode("utf-8")).digest()
    return _session_key


def _b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint_session(subject, name):
    now = int(time.time())
    payload = {"sub": subject, "name": name, "iat": now, "exp": now + SESSION_SECONDS, "jti": uuid.uuid4().hex}
    body = _b64u(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signature = _b64u(hmac.new(_key(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}", payload


def verify_session(token):
    """Return the session payload, or None when the token is missing, forged or expired."""
    if not token or token.count(".") != 1 or len(token) > 2048:
        return None
    body, signature = token.split(".")
    expected = _b64u(hmac.new(_key(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64u_decode(body))
    except ValueError:
        return None
    if not isinstance(payload, dict) or int(payload.get("exp", 0)) <= time.time():
        return None
    return payload


def login_redirect(table, nonce, acs_url):
    """Store the pending request and return the Identity Center redirect URL."""
    _entity, sso_url, _cert = idp()
    request_id = "_" + uuid.uuid4().hex
    now = int(time.time())
    table.put_item(Item={"jobId": f"saml#{request_id}", "nonce": nonce, "createdAt": now, "expiresAt": now + LOGIN_SECONDS})
    instant = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    request = (
        f'<samlp:AuthnRequest xmlns:samlp="{NS["samlp"]}" xmlns:saml="{NS["saml"]}" ID="{request_id}" Version="2.0" '
        f'IssueInstant="{instant}" Destination="{sso_url}" AssertionConsumerServiceURL="{acs_url}" '
        f'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
        f"<saml:Issuer>{SP_ENTITY_ID}</saml:Issuer></samlp:AuthnRequest>"
    )
    deflater = zlib.compressobj(9, zlib.DEFLATED, -15)
    encoded = base64.b64encode(deflater.compress(request.encode("utf-8")) + deflater.flush()).decode("ascii")
    separator = "&" if "?" in sso_url else "?"
    return f"{sso_url}{separator}{urllib.parse.urlencode({'SAMLRequest': encoded, 'RelayState': request_id})}"


def _time(value, name):
    if not value:
        raise AuthError(f"missing {name}")
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _verified_assertion(response_xml):
    try:
        return _verify_signature(response_xml)
    except SignXMLException as error:
        raise AuthError(f"signature invalid: {type(error).__name__}") from error
    except etree.XMLSyntaxError as error:
        raise AuthError("SAMLResponse is not well-formed XML") from error


def _verify_signature(response_xml):
    root = etree.fromstring(response_xml, SAFE_PARSER)
    if root.tag != f"{{{NS['samlp']}}}Response":
        raise AuthError("not a SAML Response")
    assertions = root.findall("saml:Assertion", NS)
    if len(assertions) != 1 or root.findall(".//saml:EncryptedAssertion", NS):
        raise AuthError("expected exactly one plain assertion")
    verifier = XMLVerifier()
    if assertions[0].find("ds:Signature", NS) is not None:
        signed = verifier.verify(etree.tostring(assertions[0]), x509_cert=idp()[2], ignore_ambiguous_key_info=True).signed_xml
    elif root.find("ds:Signature", NS) is not None:
        signed_root = verifier.verify(response_xml, x509_cert=idp()[2], ignore_ambiguous_key_info=True).signed_xml
        inner = signed_root.findall("saml:Assertion", NS)
        if len(inner) != 1:
            raise AuthError("signed response does not contain exactly one assertion")
        signed = inner[0]
    else:
        raise AuthError("SAML response is not signed")
    if signed.tag != f"{{{NS['saml']}}}Assertion":
        raise AuthError("signature does not cover the assertion")
    status = root.find("samlp:Status/samlp:StatusCode", NS)
    if status is None or status.get("Value") != "urn:oasis:names:tc:SAML:2.0:status:Success":
        raise AuthError(f"SAML status {status.get('Value') if status is not None else 'missing'}")
    return signed


def complete_login(table, saml_response_b64, relay_state, acs_url):
    """Validate the posted assertion and return (session token, payload, nonce)."""
    if not relay_state or not relay_state.startswith("_") or len(relay_state) > 64:
        raise AuthError("unsolicited login: start from the chat panel")
    try:
        pending = table.delete_item(Key={"jobId": f"saml#{relay_state}"}, ReturnValues="ALL_OLD",
                                    ConditionExpression="attribute_exists(jobId)").get("Attributes")
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        pending = None
    if not pending or int(pending["createdAt"]) + LOGIN_SECONDS < time.time():
        raise AuthError("login request expired or already used")

    try:
        response_xml = base64.b64decode(saml_response_b64, validate=False)
    except ValueError as error:
        raise AuthError("SAMLResponse is not base64") from error
    if len(response_xml) > 200_000:
        raise AuthError("SAMLResponse too large")
    assertion = _verified_assertion(response_xml)

    now = dt.datetime.now(dt.timezone.utc)
    issuer = assertion.findtext("saml:Issuer", namespaces=NS)
    if issuer != idp()[0]:
        raise AuthError(f"unexpected issuer {issuer}")
    conditions = assertion.find("saml:Conditions", NS)
    if conditions is None:
        raise AuthError("missing Conditions")
    if conditions.get("NotBefore") and _time(conditions.get("NotBefore"), "NotBefore") > now + CLOCK_SKEW:
        raise AuthError("assertion not yet valid")
    if _time(conditions.get("NotOnOrAfter"), "NotOnOrAfter") <= now - CLOCK_SKEW:
        raise AuthError("assertion expired")
    audiences = conditions.xpath("saml:AudienceRestriction/saml:Audience/text()", namespaces=NS)
    if SP_ENTITY_ID not in [audience.strip() for audience in audiences]:
        raise AuthError(f"audience mismatch {audiences}")

    confirmations = assertion.xpath(
        "saml:Subject/saml:SubjectConfirmation[@Method='urn:oasis:names:tc:SAML:2.0:cm:bearer']/saml:SubjectConfirmationData",
        namespaces=NS,
    )
    if not any(
        data.get("Recipient") == acs_url
        and data.get("InResponseTo") == relay_state
        and _time(data.get("NotOnOrAfter"), "SubjectConfirmationData NotOnOrAfter") > now - CLOCK_SKEW
        for data in confirmations
    ):
        raise AuthError("bearer confirmation does not match this request")

    subject = (assertion.findtext("saml:Subject/saml:NameID", namespaces=NS) or "").strip()
    if not subject or len(subject) > 256:
        raise AuthError("missing NameID")
    assertion_id = assertion.get("ID") or ""
    try:
        table.put_item(Item={"jobId": f"samlassert#{assertion_id}", "createdAt": int(time.time()),
                             "expiresAt": int(time.time()) + 24 * 3600},
                       ConditionExpression="attribute_not_exists(jobId)")
    except table.meta.client.exceptions.ConditionalCheckFailedException as error:
        raise AuthError("assertion replayed") from error

    attributes = {
        attr.get("Name"): attr.findtext("saml:AttributeValue", namespaces=NS)
        for attr in assertion.findall("saml:AttributeStatement/saml:Attribute", NS)
    }
    name = display_name(attributes) or subject
    token, payload = mint_session(subject.lower(), name)
    return token, payload, pending["nonce"], sorted(attributes)


CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def display_name(attributes):
    """Readable user name from optional SAML attributes (Identity Center attribute mappings, see README):
    familyName/givenName -> "杨志浩" for CJK names (family name first, no space) or "Sean Yang"; else displayName/name.
    The value is only shown in the chat page; the email Subject stays the identity."""
    clean = {k: " ".join(str(v or "").split())[:100] for k, v in attributes.items()}
    family, given = clean.get("familyName", ""), clean.get("givenName", "")
    if family and given:
        return f"{family}{given}" if CJK.search(family + given) else f"{given} {family}"
    return clean.get("displayName") or clean.get("name") or clean.get("username") or given or ""
