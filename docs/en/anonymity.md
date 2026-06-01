# Anonymity levels

> ← [Back to README](../../README.md)

The request headers reflected by the judge are inspected. Three levels + a sub-variant:

| Level | Name | Detection rule | Meaning |
|---|---|---|---|
| **1** | Elite | No public IP, no proxy header | Hides both your IP and the proxy's presence. |
| **2** | Anonymous | No public IP, but `via` / `x-forwarded-*` / `proxy-*` present | Hides your IP but reveals "a proxy is in use". |
| **2** + *distorting* | Distorting | L2 + a `X-Forwarded-For`-style header carries a routable public IPv4 different from yours | Hides your IP **and injects a fake IP**. Used to evade fingerprinting; risky for trust. |
| **3** | Transparent | Your public IP is reflected in headers | Doesn't hide your IP; just routes. |

`-l 1` (default) keeps elite only. `-l 2` keeps elite + anonymous (incl. distorting); `-l 3` keeps everything. The summary box reports the distorting sub-count separately.

## Distorting detection limits

The fake IP in the header must be a **public-range** IPv4 (RFC1918, loopback, link-local are filtered out). A proxy writing `0.0.0.0` or `192.168.1.1` is not distorting — just a badly-configured anonymous proxy. IPv6 or non-IP values are also out of scope.
