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
 *
 * ----------------------------------------------------------------------------
 * Optional: visit logging
 * ----------------------------------------------------------------------------
 * Set $LOG_FILE below to a writable path to enable per-visit JSONL logging.
 * proxy-profiler sends an `X-Proxyprof-Proxy: <type>://<ip>:<port>` header so
 * the log can capture the visiting proxy's claimed protocol/listen address
 * (which the judge otherwise cannot see — it only sees the ephemeral source
 * port of the outbound TCP connection).
 *
 *  - "seen_*"   = fields derived from CF / TCP layer (trusted, not spoofable
 *                 unless CF is bypassed)
 *  - "client_*" = fields parsed from X-Proxyprof-Proxy (spoofable by anyone
 *                 hitting the URL; cross-reference seen_ip if it matters)
 *
 * SECURITY: do NOT use a path inside your web root unless you also block HTTP
 * access (e.g. with .htaccess "Deny from all"). Anyone could otherwise
 * download a log of which proxies your judge has seen.
 */

// === Visit logging configuration ===
// Empty string = disabled. Examples:
//   $LOG_FILE = '/var/log/proxyjudge.log';
//   $LOG_FILE = __DIR__ . '/../proxyjudge.log';   // one level above web root
$LOG_FILE = '';

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

// X-Proxyprof-Proxy: proxyprof tarafından eklenir, formati "type://ip:port".
// Judge'ın AYRI olarak göremediği iki bilgiyi taşır: proxy'nin protokolü ve
// dinleme portu. SPOOFABLE — header'a herkes herhangi bir değer yazabilir;
// bu yüzden log'da seen_ip ile cross-reference yapılır.
$client_proxy_raw = $req_headers['X-Proxyprof-Proxy']
                 ?? $_SERVER['HTTP_X_PROXYPROF_PROXY']
                 ?? '';
$client_type = $client_ip = '';
$client_port = 0;
if ($client_proxy_raw !== '') {
    if (preg_match(
        '#^(http|https|socks4|socks5)://([\d.]+):(\d{1,5})$#i',
        $client_proxy_raw, $m,
    )) {
        $client_type = strtolower($m[1]);
        $client_ip   = $m[2];
        $client_port = (int) $m[3];
        if ($client_port < 1 || $client_port > 65535) {
            $client_port = 0;
            $client_ip = '';
            $client_type = '';
        }
    }
}

// Strip every CF-* header so they don't surface in the dump. Without this, the
// CF-* keys would NOT trigger detect_level()'s PROXY_HEADERS check (they're
// not in that list), but they'd still clutter the output and reveal the judge
// is CF-backed. Keep the response clean.
foreach (array_keys($req_headers) as $k) {
    if (stripos($k, 'cf-') === 0) {
        unset($req_headers[$k]);
    }
}
// Also drop CDN-Loop (CF adds it to prevent loops) and X-Proxyprof-* internal
// headers — they'd otherwise show up in the response and could even trip the
// proxy-header detection on some custom detectors. The *original*
// X-Forwarded-For from the proxy is preserved; only judge-internal noise is
// stripped here.
unset($req_headers['CDN-Loop']);
foreach (array_keys($req_headers) as $k) {
    if (stripos($k, 'x-proxyprof-') === 0) {
        unset($req_headers[$k]);
    }
}

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

// ----------------------------------------------------------------------------
// Visit logging — opt-in via $LOG_FILE at top of file.
// One JSON record per line (JSONL). Append-only with flock to serialise
// concurrent writers. Logging failures are silenced — the judge response is
// already flushed to the client by this point and must not be affected.
// ----------------------------------------------------------------------------
if ($LOG_FILE !== '') {
    $entry = [
        'ts'          => gmdate('c'),                          // ISO 8601 UTC
        'seen_ip'     => $cf_ip ?: ($_SERVER['REMOTE_ADDR'] ?? ''),
        'seen_port'   => (int) ($_SERVER['REMOTE_PORT'] ?? 0),
        'country'     => $cf_country ?: null,
        'client_type' => $client_type ?: null,
        'client_ip'   => $client_ip ?: null,
        'client_port' => $client_port ?: null,
        // UA is truncated — proxies often inject huge fingerprinting strings.
        'ua'          => substr($req_headers['User-Agent']
                                ?? $_SERVER['HTTP_USER_AGENT'] ?? '', 0, 200),
        'cf_ray'      => $cf_ray ?: null,
    ];
    $line = json_encode(
        $entry,
        JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE,
    );
    if ($line !== false) {
        $fh = @fopen($LOG_FILE, 'ab');
        if ($fh !== false) {
            if (@flock($fh, LOCK_EX)) {
                @fwrite($fh, $line . "\n");
                @flock($fh, LOCK_UN);
            }
            @fclose($fh);
        }
    }
}
