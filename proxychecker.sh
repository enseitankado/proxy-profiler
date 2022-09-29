#!/bin/bash
cd /home/pi/proxine

# Collect, merge and sort proxies: Level-1
./proxine.sh socks5 | php /home/pi/proxy-profiler/proxyprof.php -t socks5 -l 1 -g -o /home/pi/proxine/proxy/socks5.txt -s
git add .; git commit -m "`cat proxy/socks5.txt | wc -l` working elite proxies added."; git push -f

./proxine.sh socks4 | php /home/pi/proxy-profiler/proxyprof.php -t socks4 -l 1 -g -o /home/pi/proxine/proxy/socks4.txt -s
git add .; git commit -m "`cat proxy/socks4.txt | wc -l` working elite proxies added."; git push -f

./proxine.sh https | php /home/pi/proxy-profiler/proxyprof.php -t https -l 1 -g -o /home/pi/proxine/proxy/https.txt -n 1000 -s
git add .; git commit -m "`cat proxy/https.txt | wc -l` working elite proxies added."; git push -f

./proxine.sh http | php /home/pi/proxy-profiler/proxyprof.php -t http -l 1 -g -o /home/pi/proxine/proxy/http.txt -n 1000 -s
git add .; git commit -m "`cat proxy/http.txt | wc -l` working elite proxies added."; git push -f


# CloudFlare approved Level-1 proxies
php /home/pi/proxy-profiler/proxyprof.php -f socks5.txt -t socks5 -l 1 -g -o /home/pi/proxine/proxy-cloudflare-pass/socks5.txt -s -a https://www.tankado.com/
git add .; git commit -m "`cat proxy-cloudflare-pass/socks5.txt | wc -l` CloudFlare approved elite proxies added."; git push -f

php /home/pi/proxy-profiler/proxyprof.php -f socks4.txt -t socks4 -l 1 -g -o /home/pi/proxine/proxy-cloudflare-pass/socks4.txt -s -a https://www.tankado.com/
git add .; git commit -m "`cat proxy-cloudflare-pass/socks4.txt | wc -l` CloudFlare approved elite proxies added."; git push -f

php /home/pi/proxy-profiler/proxyprof.php -f https.txt -t https -l 1 -g -o /home/pi/proxine/proxy-cloudflare-pass/https.txt -n 1000 -s -a https://www.tankado.com/
git add .; git commit -m "`cat proxy-cloudflare-pass/https.txt | wc -l` CloudFlare approved elite proxies added."; git push -f

php /home/pi/proxy-profiler/proxyprof.php -f http.txt -t http -l 1 -g -o /home/pi/proxine/proxy-cloudflare-pass/http.txt -n 1000 -s -a https://www.tankado.com/
git add .; git commit -m "`cat proxy-cloudflare-pass/http.txt | wc -l` CloudFlare approved elite proxies added."; git push -f
