$ php proxyprof.php

 Author ozgurkoca: github.com/enseitankado/

 -h                     Show this help.
 -p <host:ip>           Check proxy(s). Comma supported.
 -t <type>              Proxy types: http/https, socks4, socks5
 -f <file_name>         Input proxy file. Each line has an IP:port
 -o <file_name/STDOUT>  Output proxy file or STDOUT ('STDOUT' is casesensitive).
 -l <level>             Min proxy level output filter. Defeault: 3
 -n <num>               Thread count. Default: 250
 -c <secs>              Connection timeout. Default: 5 secs.
 -a <URL>               Blocking test URL. Ex. cloudflare.com protected web service.
 -j <URL>               Judge URL. Default: random azenv.php
 -s                     Silent. No output.
 -g                     List only good proxies.
 -r                     Show progress bar.

 Current Installation:

   PHP 7.4.20
   CURL 7.70.0
   OpenSSL 1.1.1k  25 Mar 2021

 About proxy levels:

   1: Elite proxy servers hide both your IP address and the fact that you are using a proxy server at all.
   2: An anonymous proxy does not reveal your IP address but does reveal that you are using a proxy server.
   3: Transparent proxies do not hide your IP Address and they don’t alter any user information.





If input proxy file and output name is same and output file not empty then output list merged into input proxy file and will be scanned alltogether again.


The main difference that separates these three proxy types is the level of security and privacy they offer.

    Transparent proxies do not hide your IP Address and they don’t alter any user information.
    An anonymous proxy does not reveal your IP address but does reveal that you are using a proxy server.
    Elite proxy servers hide both your IP address and the fact that you are using a proxy server at all.

For the most protection, while browsing the internet, an elite proxy is the best choice. That doesn’t mean transparent and anonymous don’t have their uses. Public elite proxies are more overloaded than transparent servers, so if you were looking for something that loads pages faster but aren’t concerned about privacy, then a transparent proxy would be the best choice. The best proxy option is determined by your needs.


Donation

If you want to buy my coffee, you can send payments Paypal.

Donate

Disclaimer

This is an open source for everyone, you may redistribute, modify, use patents and use privately without any obligation to redistribute. but it should be noted to include the source code of the library that was modified (not the source code of the entire program), include the license, include the original copyright of the author (warifp), and include any changes made (if modified). Users do not have the right to sue the creator when there is damage to the software or even demand if there is a problem caused by the makers of this tool. because every risk is caused by the user risk itself.


HTTPS proxies generally support HTTP, but not vice versa.

https://github.com/php-curl-class
https://github.com/guiguiboy/PHP-CLI-Progress-Bar


SOCKS is a layer 5 protocol, and it doesn’t care about anything below that layer in the Open Systems Interconnection (OSI) model — meaning you can’t use it to tunnel protocols operating below layer 5. This includes things such as ping, Address Resolution Protocol (ARP), etc. From a security perspective, it won’t allow an attacker to perform scans using tools such as Nmap if they are scanning based on half-open connections because it works at layer 5.

Since SOCKS sits at layer 5, between SSL (layer 7) and TCP/UDP (layer 4), it can handle several request types, including HTTP, HTTPS, POP3, SMTP and FTP. As a result, SOCKS can be used for email, web browsing, peer-to-peer sharing, file transfers and more.

Other proxies built for specific protocols at layer 7, such as an HTTP proxy that is used to interpret and forward HTTP or HTTPS traffic between client and server, are often referred to as application proxies.
There are only two versions: SOCKS4 and SOCKs5. The main differences between SOCKs5 and SOCKS4 are:

SOCKS4 doesn’t support authentication, while SOCKs5 supports a variety of authentication methods; and
SOCKS4 doesn’t support UDP proxies, while SOCKs5 does.
A SOCKs5 proxy is more secure because it establishes a full TCP connection with authentication and uses the Secure Shell (SSH) encrypted tunneling method to relay the traffic.



// If needed, start a SOCKS 5 proxy tunnel:
//   $ ssh -D 8080 -C -N -v user@example.com



printf "31.44.82.182:5678\n185.139.56.133:4145" | php proxycheck.php -t socks4 -n 1000
cat socks4.lst | php proxycheck.php -t socks4 -n 1000 -g



    SOCKS5 supports multiple authentication methods, SOCKS4 does not support authentication;
    SOCKS5 supports UDP proxy, SOCKS4 does not support UDP proxy;
    SOCKS5 is more secure because it uses an authenticated TCP connections and SSH encrypted tunnels;