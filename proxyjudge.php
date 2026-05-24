<?php
/**
 * proxyjudge.php — Cloudflare-aware judge for proxy-profiler
 *
 * Deploy this file behind a Cloudflare-proxied domain. Cloudflare injects
 *  - CF-Connecting-IP   : actual client IP (the proxy's outbound address)
 *  - CF-IPCountry       : ISO-3166-1 alpha-2 country code (e.g. "TR", "US")
 *  - CF-Ray             : edge trace ID
 *
 * Without normalisation, the request that reaches the origin server has
 * REMOTE_ADDR == Cloudflare edge IP (not what we want) and every probe would
 * look "transparent" because CF-Connecting-IP carries the client's IP.
 *
 * What this script does:
 *  1. Replace REMOTE_ADDR with CF-Connecting-IP so anonymity detection works
 *     against the *real* client IP as seen by Cloudflare.
 *  2. Surface country code as a dedicated PROXY_COUNTRY line — proxy-profiler
 *     picks it up automatically (no extra GeoIP lookup needed).
 *  3. STRIP all CF-* headers from the dump so they don't trip the
 *     proxy-header detector and inflate anonymity downgrades.
 *  4. Emit the classic <pre>KEY = VALUE</pre> format compatible with
 *     proxy-profiler's parser (judges.py:parse_judge_response).
 *
 * Deployment (any PHP-capable host behind CF orange-cloud):
 *   - Drop this file at e.g. /proxyjudge.php
 *   - Test from a browser: curl -i https://yourdomain.tld/proxyjudge.php
 *   - Use with proxyprof:  proxyprof http -j https://yourdomain.tld/proxyjudge.php
 *
 * Note: requires CF "orange cloud" mode (proxied). DNS-only mode (gray cloud)
 * won't add the CF-* headers and you'll get a vanilla judge response.
 */

header('Content-Type: text/html; charset=utf-8');
header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');
header('Pragma: no-cache');

// Collect every request header. apache_request_headers() is preferred (preserves
// case + non-HTTP_-prefixed names) but FastCGI / nginx-php-fpm builds may not
// provide it — fall back to reconstructing from $_SERVER.
if (function_exists('apache_request_headers')) {
    $req_headers = apache_request_headers();
} else {
    $req_headers = [];
    foreach ($_SERVER as $k => $v) {
        if (strpos($k, 'HTTP_') === 0) {
            $name = str_replace('_', '-', substr($k, 5));
            $name = ucwords(strtolower($name), '-');
            $req_headers[$name] = $v;
        }
    }
}

// Cloudflare-specific extraction. CF guarantees these are added before the
// request hits origin (server-side), so they cannot be spoofed by the client
// unless CF is bypassed.
$cf_ip       = $req_headers['CF-Connecting-IP'] ?? $_SERVER['HTTP_CF_CONNECTING_IP'] ?? '';
$cf_country  = $req_headers['CF-IPCountry']     ?? $_SERVER['HTTP_CF_IPCOUNTRY']     ?? '';
$cf_ray      = $req_headers['CF-Ray']           ?? $_SERVER['HTTP_CF_RAY']           ?? '';
$cf_proto    = $req_headers['CF-Visitor']       ?? $_SERVER['HTTP_CF_VISITOR']       ?? '';
$cf_asn      = $req_headers['CF-Connecting-IP-Country'] ?? '';

// Strip every CF-* header so they don't surface in the dump. Without this, the
// CF-* keys would NOT trigger detect_level()'s PROXY_HEADERS check (they're
// not in that list), but they'd still clutter the output and reveal the judge
// is CF-backed. Keep the response clean.
foreach (array_keys($req_headers) as $k) {
    if (stripos($k, 'cf-') === 0) {
        unset($req_headers[$k]);
    }
}
// Also drop CDN-Loop (CF adds it to prevent loops) and X-Forwarded-* values
// CF appended automatically — the *original* X-Forwarded-For from the proxy
// is preserved, only CF's appended entries would inflate the chain. We keep
// the header as-is to mirror what classic judges report.
unset($req_headers['CDN-Loop']);

// Assemble normalised dump. REMOTE_ADDR uses CF-Connecting-IP when present;
// otherwise falls back to the actual TCP peer (only meaningful when this
// script is deployed without CF, e.g. for local testing).
$dump = [];
$dump['REMOTE_ADDR']   = $cf_ip ?: ($_SERVER['REMOTE_ADDR'] ?? '');
$dump['REMOTE_PORT']   = $_SERVER['REMOTE_PORT'] ?? '';
$dump['PROXY_COUNTRY'] = $cf_country;   // picked up by proxyprof
if ($cf_ray)   $dump['PROXY_CF_RAY']   = $cf_ray;    // optional trace id
if ($cf_proto) $dump['PROXY_CF_VISITOR'] = $cf_proto;

foreach ($req_headers as $k => $v) {
    if (is_array($v)) {
        $v = implode(', ', $v);
    }
    // Uppercase + underscore so the line shape matches classic azenv.php
    // output ("HTTP_X_FORWARDED_FOR = ..."), which proxy-profiler parses
    // case-insensitively into lowercase keys.
    $key = strtoupper(str_replace('-', '_', $k));
    // apache_request_headers gives bare names ("User-Agent"); $_SERVER gave
    // us already-stripped names. Both end up uppercase with underscores;
    // prefix with HTTP_ if it's a request header (not the synthetic ones).
    if (strpos($key, 'HTTP_') !== 0 && !isset($dump[$key])) {
        $key = 'HTTP_' . $key;
    }
    $dump[$key] = $v;
}

echo "<pre>\n";
foreach ($dump as $k => $v) {
    // Sanitise so a malicious header can't inject HTML into the <pre> block.
    echo htmlspecialchars($k, ENT_QUOTES, 'UTF-8')
       . ' = '
       . htmlspecialchars((string)$v, ENT_QUOTES, 'UTF-8')
       . "\n";
}
echo "</pre>\n";
