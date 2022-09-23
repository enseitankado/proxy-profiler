
# NextGen Proxy Profiler

The scanner (proxyprof) scans and analyzes **http**/**https**/**socks4**/**socks5** proxies quickly. It can complete thousands of proxy scans in seconds by running the Curl tool multi-threaded in the background.

***Some interesting features:***

 - Detects security level of the proxy. Elite, Anon or Transparent.
 - Checks if the proxy has permission from any firewall.
 - Receives input from STDIN and transmits to STDOUT which allows
   chaining with other tools.
   
***Some features to add in future***
 - Tunneling support checking.
 - Private proxy profiling with un:pw.
 - More comprohensive black list checking.
   
# Requirements
PHP, Curl, PHP-Curl extension, [PHP-Curl class](https://github.com/php-curl-class), [PHP-CLI-Progress-Bar](https://github.com/guiguiboy/PHP-CLI-Progress-Bar)
> Note: proxyprof use modified version of PHP-Curl class so do not update the library.

# Installation

```bash
    $ git clone https://github.com/enseitankado/proxy-profiler.git
	$ cd proxy-profiler
	$ php proxyprof.php -h
  
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
```

proxyprof is PHP-Cli tool and requires some librarys. When you run the tool for the first time, it tests whether the required plug-ins are installed. If it is not installed, it stops working and lists the tools that need to be installed. For example:

```bash
    $ php proxyprof.php
    
     Multibyte string (mb_) library not installed !
     To install and enable the library run commands below:
    
       sudo apt install php7.4-mbstring
       sudo phpenmod -v 7.4 mbstring
    
    
     PHP curl library not installed !
     To install and enable the library run commands below:
    
       sudo apt install php7.4-curl
       sudo phpenmod -v 7.4 curl
```


# Command Line Arguments
 **-h**
 Show this help.
 
 **-p \<host:ip>**
 It is used to specify comma-separated IP:PORT pairs of proxy servers. Check example below.
 
 **-t \<type>**
  Specifiy proxy types: http/https, socks4 or socks5.
 
 **-f \<file_name>**
  Input proxy file. Each line has an IP:PORT format. 
 

> **Hint:** If input proxy file and output name is same and output file not empty then output list merged into input proxy file and will be
> scanned alltogether again.

 
 **-o \<file_name/STDOUT>**
 Output proxy file or STDOUT ('STDOUT' is casesensitive).  Specifies the name of the file to save the scan results. Proxy servers that meet the scanning criteria are saved in this file. Proxy servers are registered in the same format (IP:PORT) as recorded in the input file (-f).

> **Hint:** If STDOUT is written as the filename, the scan result is simply redirected to standard output. That is, it is printed on the
> screen. This is a plain output with one IP:PORT per line. This feature
> is useful for performing chained command executions where the output
> of proxyprof is routed as input to another tool.

 **-l \<level>**
 Min proxy level output filter. The default value is 3. After establishing a TCP connection to the proxy, the security level is tried to be determined. To determine the security level, the script azenv.php is used, an example of which is also in this repository. Using the scanned proxy, a request is made to one of the internet copies of the script assigned as the judge and the HTTP headers reaching the target are scanned. The security levels of the Rated Proxies are listed below. The main difference that separates these three proxy types is the level of security and privacy they offer. 

> **Hint:** When proxyprof is used with the -g (good) option, only servers with the minimum security level specified by this parameter
> are included in the result set.  
> 
> **Proxy Security Levels** 
> Level 3: Transparent proxies do not hide your IP Address and they don’t alter   any user information.   Level
> 2: An anonymous proxy does not reveal your IP address but does reveal
> that you are using a proxy server. Level 1: Elite proxy servers hide
> both your IP address and the fact that you are using a proxy server at
> all.

 
 **-n \<num>**
 Thread count. Default: 250. proxyprof can perform multiple simultaneous scans with low resource requirements. This saves a lot of time, especially when tens of thousands of proxies need to be scanned. 

> **Hint 1:** The firewall on your line may be configured against opening 
> a large number of TCP connections in a short time. Especially when scanning 
> SOCKS4/SOCKS5 servers, it opens a lot of sockets in a short time. 
> If you get a meaningless amount of failed scan results, try again 
> by reducing the number of threads. For example, set the number of threads 
> between 1-5. The default value is 250.

> **Hint 2:** If you get PHP memory allocate error, reduce the number of threads or 
> increase the memory limit that can be used with the -d parameter 
> of the php interpreter. 
> For example php -d memory_limit=500MB script_to_run.php
 
 **-c \<secs>**
 Connection timeout. Default: 5 secs. During the handshake process with a proxy, it waits for a certain maximum amount of time to respond to the connection request. Otherwise, the connection will remain open for a long time, causing excessive resource consumption and reduced browsing speed. If the time defined by this parameter is exceeded, it is judged that the proxy is not responding. A good proxy should respond instantly and quickly.
 
 **-a \<URL>**
 Blocking test URL. Ex. cloudflare.com protected web service. Especially since proxy servers open to public use are used by many people for many different purposes, they can easily fall into the blacklists of protection systems. This option checks if the proxy is blacklisted by a particular network/server. An attempt is made to connect to the URL address specified with this parameter using a proxy, and at the end the result is listed as blocked or unblocked. This option becomes a mandatory condition when used with -g (good) and only unblocked addresses are included in the result set.

 **-j \<URL>**               
 Judge URL. Default: random azenv.php. The Judge URL is accessed using the proxy and reflects back client information that the proxy carries with http headers. There are already many judge URLs in proxyprof's list and choose one of them at the start of the scan. You can see a list of Judge URLs in the source code. If you want to use a custom Judge url use this parameter. A judge file that proxyprof is compatible with has been shared in the repository with the name [azenv.php](https://github.com/enseitankado/proxy-profiler/blob/main/azenv.php).
 
 **-s**                     
 Silent. No any output.
 
 **-g**
 List only good proxies. This means scan results match with scan criterias such as timeout, proxy security level and blocking test results. 
 
 **-r**
 Show progress bar.


# Examples
Scans socks4 proxy list from socks4.lst file and display scan results.
```bash
    $ php proxyprof.php -t https -f socks4.lst
```

Scans https proxy list from https.lst file and display scan results, save good proxies to goodhttps.lst in IP:PORT format.
```bash
    $ php proxyprof.php -t https -f https.lst -o goodhttps.lst
```

Scans https proxy list from https.lst file and output good proxies to standart display in IP:PORT format.
```bash
    $ php proxyprof.php -t https -f https.lst -o STDOUT 
```

Scan HTTPS proxy server list in multithreaded (1500 thread) with 20 seconds timeout delay. If proxy is up then reach to URL "https://www.tankado.com/" behind the CloudFlare (CF) network and test blocking or not blocking by CF. Displays only good servers with non blocked in a table. 
```bash
    $ php proxyprof.php -t https -f https.lst -g -n 1500 -a https://www.tankado.com/ -c 20
```

If needed, start a SOCKS-5 proxy tunnel: 
```bash
    $ ssh -D 8080 -C -N -v  user@example.com
```

Scan two proxy:
```bash
    $ php proxyprof.php -t socks4 -p 103.105.41.209:4145,194.27.16.17:62013   
```
 
Use STDIN to input scan list example-1:
```bash
    $ printf "31.44.82.182:5678\n185.139.56.133:4145" | php proxycheck.php -t socks4 -n 1000 
```	

Use STDIN to input scan list example-2:
```bash
    $ cat socks4.lst | php proxycheck.php -t socks4 -n 1000 -g
```


# Disclaimer

This is an open source for everyone, you may redistribute, modify, use patents and use privately without any obligation to redistribute. but it should be noted to include the source code of the library that was modified (not the source code of the entire program), include the license, include the original copyright of the author (warifp), and include any changes made (if modified). Users do not have the right to sue the creator when there is damage to the software or even demand if there is a problem caused by the makers of this tool. because every risk is caused by the user risk itself.

# About Proxies
## HTTP proxies
HTTPS proxies generally support HTTP, but not vice versa. For the most protection, while browsing the internet, an elite proxy is the best choice. That doesn’t mean transparent and anonymous don’t have their uses. Public elite proxies are more overloaded than transparent servers, so if you were looking for something that loads pages faster but aren’t concerned about privacy, then a transparent proxy would be the best choice. The best proxy option is determined by your needs.

## SOCKS4 and SOCKS5 proxies
SOCKS is a layer 5 protocol, and it doesn’t care about anything below that layer in the Open Systems Interconnection (OSI) model — meaning you can’t use it to tunnel protocols operating below layer 5. This includes things such as ping, Address Resolution Protocol (ARP), etc. From a security perspective, it won’t allow an attacker to perform scans using tools such as Nmap if they are scanning based on half-open connections because it works at layer 5.

Since SOCKS sits at layer 5, between SSL (layer 7) and TCP/UDP (layer 4), it can handle several request types, including HTTP, HTTPS, POP3, SMTP and FTP. As a result, SOCKS can be used for email, web browsing, peer-to-peer sharing, file transfers and more.

Other proxies built for specific protocols at layer 7, such as an HTTP proxy that is used to interpret and forward HTTP or HTTPS traffic between client and server, are often referred to as application proxies. There are only two versions: SOCKS4 and SOCKs5. The main differences between SOCKs5 and SOCKS4 are:

SOCKS4 doesn’t support authentication, while SOCKs5 supports a variety of authentication methods; and SOCKS4 doesn’t support UDP proxies, while SOCKs5 does. A SOCKs5 proxy is more secure because it establishes a full TCP connection with authentication and uses the Secure Shell (SSH) encrypted tunneling method to relay the traffic.

SOCKS5 supports multiple authentication methods, SOCKS4 does not support authentication;
SOCKS5 supports UDP proxy, SOCKS4 does not support UDP proxy;
SOCKS5 is more secure because it uses an authenticated TCP connections and SSH encrypted tunnels;

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=enseitankado/proxy-profiler&type=Date)](https://star-history.com/#enseitankado/proxy-profiler&Date)

# Donation

Would you like to buy me a coffee? [Click](https://www.buymeacoffee.com/ozgurkoca).

# Author

I'm Özgür. I'm a teacher at a vocational [school](https://samsuneml.meb.k12.tr/)
Repos: https://github.com/enseitankado
Blog: www.tankado.com