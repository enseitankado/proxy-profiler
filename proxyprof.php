<?php
	/**
	 * This tool profiles HTTP/SOCKS proxy(s) as multithreaded.
	 *
	 * @homepage   https://github.com/enseitankado/awesome-proxies
	 * @package    PHP Proxy Checker/Profiler (HTTP,HTTPS,SOCKS4/5)
	 * @subpackage Common
	 * @license    http://opensource.org/licenses/gpl-license.php  GNU Public License
	 * @author     Ozgur KOCA <ozgurkoca@gmail.com>
	 */ 
	error_reporting(E_ERROR & ~E_WARNING & ~E_PARSE);
	foreach (glob(__DIR__ . '/lib/*.php') as $filename)
		require_once $filename;
	foreach (glob(__DIR__ . '/lib/php-curl-class/src/Curl/*.php') as $filename)
		require_once $filename;
	
	use Curl\Curl;
	use Curl\MultiCurl;	
	$start = microtime(true);
	
	# Check prequistics
	check_prequistics();

	# Command line options
	$opts['h'] = array("-h", 				"\t\t\tShow this help.");
	$opts['p:'] = array("-p <host:ip>", 	"\t\tCheck proxy(s). Comma supported.");
	$opts['t:'] = array("-t <type>", 		"\t\tProxy types: http/https, socks4, socks5");	
	$opts['f:'] = array("-f <file_name>", 	"\t\tInput proxy file. Each line has an IP:port. STDIN supported.");
	$opts['o:'] = array("-o <file_name/STDOUT>", 	"\tOutput proxy file or STDOUT ('STDOUT' is casesensitive).");
	$opts['l:'] = array("-l <level>", 		"\t\tMin proxy level output filter. Defeault: 3");
	$opts['n:'] = array("-n <num>", 		"\t\tThread count. Default: 250");
	$opts['c:'] = array("-c <secs>", 		"\t\tConnection timeout. Default: 5 secs.");	
	$opts['a:'] = array("-a <URL>", 		"\t\tBlocking test URL. Ex. cloudflare.com protected web service.");
	$opts['j:'] = array("-j <URL>", 		"\t\tJudge URL. Default: random azenv.php");
	$opts['y:'] = array("-y <num>", 		"\t\tMaximum retry count if connection failed. Default: 1");
	$opts['m']  = array("", 				"\t\tDont merge output list into input list.");
	$opts['s'] 	= array("-s", 				"\t\t\tSilent. No output.");	
	$opts['g'] 	= array("-g", 				"\t\t\tList only good proxies.");
	$opts['r'] 	= array("-r", 				"\t\t\tShow progress bar.");
		
	$cmd = getopt(implode('', array_keys($opts)), array());	

	# Display help
	if (isset($cmd['h']) or !count($cmd)) {
		echo "\n Author ozgurkoca: github.com/enseitankado/\n\n";
		foreach($opts as $opt => $opt_arr)
			echo ' '.$opt_arr[0].$opt_arr[1]."\n";			
			echo "\n Current Installation:\n\n";
			echo "   PHP ".phpversion().", Memory Limit: ".return_bytes(ini_get('memory_limit'))."\n";
			echo "   CURL ".curl_version()["version"]."\n";
			echo "   ".OPENSSL_VERSION_TEXT."\n";
			echo "\n About proxy levels:\n\n";
			echo "   1: Elite proxy servers hide both your IP address and the fact that you are using a proxy server at all.\n";			
			echo "   2: An anonymous proxy does not reveal your IP address but does reveal that you are using a proxy server.\n";
			echo "   3: Transparent proxies do not hide your IP Address and they don’t alter any user information.\n\n";	
		exit(0);
	}
	
	if (function_exists("posix_isatty"))
	if (!posix_isatty(STDIN))
		$cmd['stdin'] = file_get_contents('php://stdin');
	
	# Scan proxies
	if ((isset($cmd['p']) or isset($cmd['f']) or isset($cmd['stdin'])) and isset($cmd['t'])) {
		check_proxies($cmd);
	} else 
		die("\nInput list doesnt provided. Try help (-h).\n");
	
	//****************************************************************

	/**
	 * The main function to checks and profiles proxy(ies)
	 *
	 * @param array $cmd Command line arguments
	 * @return No return.
	 */	
	function check_proxies(&$cmd) {
		
		# Parse arguments and initialize		
		$proxy_arr 		= is_array($cmd['p']) 	? $cmd['p'] : array($cmd['p']);		
		$thread_count 	= isset($cmd['n']) 		? $cmd['n'] : 250;
		$time_out 		= isset($cmd['c']) 		? $cmd['c'] : 5;		
		$min_level		= isset($cmd['l']) 		? $cmd['l'] : 3;
		$max_retries	= isset($cmd['y']) 		? $cmd['y'] : 1;
		$proxy_type 	= strtoupper($cmd['t']);
		$user_agent 	= 'Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1; SV1)';
		$proxy_list		= build_proxy_list($cmd);
		$req_url		= !isset($cmd['a']) ? get_judge_url($cmd) : $cmd['a'];
		$run_mode		= !isset($cmd['a']) ? 'Judge mode' : 'Blocking test';

		$cmd['time_out'] = $time_out;
		$cmd['bad_count'] = $cmd['good_count'] = 0;		
		$cmd['public_ip'] = get_public_ip();
		$cmd['min_proxy_level'] = $min_level;

		# Merge output file into input proxy list
		if (!isset($cmd['m']))
		if (isset($cmd['f']) and isset($cmd['o']) and $cmd['o'] != 'STDOUT') {
			if ($cmd['f'] != $cmd['o']) {
				if (file_exists($cmd['o'])) {
					$out_proxy_list = array_map('trim', 
										explode("\n", 
											trim(file_get_contents($cmd['o']))));											
					$proxy_list = array_merge($proxy_list, $out_proxy_list);
					$proxy_list = array_unique($proxy_list);
				}
			}
		}
		
		# Dont display cosmetics
		if ($cmd['o'] == 'STDOUT')
			$cmd['s'] = true;
		
		# Print scan configurations
		if (!isset($cmd['s'])) {			
			echo "\n Current configuration:\n\n";
			echo "   Your Public IP \t= {$cmd['public_ip']}\n";
			echo "   Run mode \t\t= $run_mode\n";			
			echo "   Request URL \t\t= $req_url\n";			
			echo "   Proxy Type \t\t= $proxy_type\n";
			if (!isset($cmd['a']))
			echo "   Min.ProxyLevel \t= $min_level\n";
			echo "   Timeout \t\t= $time_out seconds\n";
			echo "   Retry count \t\t= $max_retries\n";			
			echo "   Input proxy \t\t= ".count($proxy_list)."\n";
			echo "   Thread count \t= $thread_count\n";
		}
	
		# Progress bar
		if (isset($cmd['r']) and !isset($cmd['s'])) {
			global $progressBar;
			$progressBar = new \ProgressBar\Manager(0, count($proxy_list), 100);
			$progressBar->setFormat('%current%/%max% [%bar%] %eta% Good: %good%');		
			$progressBar->addReplacementRule('%good%', 70, function ($buffer, $registry) {global $cmd; return $cmd['good_count'];});
		}
		
		# Table Header: Scan results
		if (!isset($cmd['s']) and !isset($cmd['r'])) { # No silent, No progress
			echo "\n";
			$mask = "%7.7s |%5.5s | %21.21s | %7.7s | %17.17s | %6.6s | %5.5s | %11.11s | %s \n";		
			printf($mask, 'Num', 'Stat', 'Proxy', 'Type', 'Proxy Outbound IP', 'Level', 'Time', 'Blocking', 'Info');
			echo " ".str_repeat('~', 155)."\n";
		}
		
		# To reduce memory comsumption slice the list to chunks
		$chunked_proxies = array_chunk($proxy_list, $thread_count);
		foreach ($chunked_proxies as $proxies) {
		
			# --------------
			# Init MultiCurl
			# --------------
			$multi_curl = new MultiCurl();
			$multi_curl->setUserAgent(random_user_agent());
			$multi_curl->setConnectTimeout($time_out);
			$multi_curl->setConcurrency($thread_count);
			$multi_curl->setRetry($max_retries);
			$multi_curl->setOpt(CURLOPT_TIMEOUT, $time_out);		
			$multi_curl->setOpt(CURLOPT_FOLLOWLOCATION, 1);		
			$multi_curl->setOpt(CURLOPT_SSL_VERIFYHOST, 0);
			$multi_curl->setOpt(CURLOPT_SSL_VERIFYPEER, 0);
			$multi_curl->setOpt(CURLOPT_SSL_VERIFYSTATUS, 0);
			//$multi_curl->setOpt(CURLOPT_HEADER, 1); # Debug
			//$multi_curl->setOpt(CURLOPT_RETURNTRANSFER, 0); # Debug		
			$multi_curl->setProxyType(get_curl_proxy_type($cmd));
			
			$multi_curl->setProxies($proxies); 		// Modified: Assign random proxies to every curl instances droped.
			$proxy_count = count($proxies);
			
			for ($i=0; $i<$proxy_count; $i++)  		// Load judge URLs		
				$multi_curl->addGet($req_url);
					
			# ---------------
			# Define listener 
			# ---------------
			$multi_curl->complete(function ($instance) {		
				
				global $cmd, $goods;
				$blocking = 'Not tested';
				$curl_info = $instance->getInfo();
				$url = $instance->url;			
				$proxy_type =  strtoupper($cmd['t']);
				$proxy_ip_port = $instance->getOpt(CURLOPT_PROXY);
				$proxy_id = ++$cmd['proxy_id'];
				
				# --------------------------------
				# Bad proxy(s), connection failure
				# --------------------------------	
				if ($instance->curlError or $instance->curlErrorCode) {
					$status = 'Bad';
					$cmd['info'] = 'Error: ' . $instance->errorCode . ', ' . $instance->errorMessage.'.';									
					$cmd['bad_count']++;
					$cmd['time'] = round($curl_info['total_time_us']/1000000, 1);
					$proxy_outbound_ip = '-';
					$proxy_level = '-';
					
				# -------------------------
				# Good proxy(s)
				# -------------------------
				} else {
					global $cmd;
					
					// Judge mode
					if (!isset($cmd['a'])) {
						$judge_headers = build_proxied_response_header($instance->rawResponse);																
						$proxy_outbound_ip = $judge_headers['remote_addr'];				
						$proxy_level = get_proxy_level($judge_headers, $public_ip);
						$cmd['info'] = get_proxy_infrastructure($judge_headers, $proxy_ip_port, $public_ip);						
						if ($proxy_level > $cmd['min_proxy_level']) {
							$cmd['info'] = 'Proxy security level is '.$proxy_level;
							$status = 'Bad';
						}
						$status = 'Good';
						$goods[] = $proxy_ip_port;
						$cmd['good_count'] = count($goods);
							
					// Approve mode
					} else {
						
						$proxy_outbound_ip = '-';
						$proxy_level = '-';
						
						// Http connection blocked
						if ($instance->httpError or $instance->error) {						
							$proxy_outbound_ip = '-';
							$proxy_level = '-';
							$cmd['info'] = $instance->errorMessage;
							$status = 'Bad';
							$blocking = 'Yes';
						} else {
							// Good, connection approved							
							$goods[] = $proxy_ip_port;
							$cmd['good_count'] = count($goods);
							$cmd['info'] = 'Approved URL: '.$cmd['a'];
							$status = 'Good';
							$blocking = 'No';							
						}
					}					
					
					$cmd['time'] = round($curl_info['total_time_us']/1000000, 1);						
					
					# Stdout good proxy(s)
					if ($cmd['o'] == 'STDOUT') 						
						echo $proxy_ip_port.PHP_EOL;					
				}
				
				# Table: Scan results
				$time = $cmd['time'];
				$mask = "%7.7s |%5.5s | %21.21s | %7.7s | %17.17s | %6.6s | %5.5s | %11.11s | %s \n";
				if (!isset($cmd['s']) and !isset($cmd['r'])) # No silent, No progress
					if (isset($cmd['g'])) { # Only goods
						if ($status == 'Good')
							if (isset($cmd['a'])) { # Access test
								if ($blocking == 'No')
									printf($mask, $cmd['good_count'], $status, $proxy_ip_port, $proxy_type, $proxy_outbound_ip, $proxy_level, $time, $blocking, $cmd['info']);
							} else
								printf($mask, $cmd['good_count'], $status, $proxy_ip_port, $proxy_type, $proxy_outbound_ip, $proxy_level, $time, $blocking, $cmd['info']);
					} else # Goods and Bads
						printf($mask, $proxy_id, $status, $proxy_ip_port, $proxy_type, $proxy_outbound_ip, $proxy_level, $time, $blocking, $cmd['info']);
				
				# Progress bar
				if (isset($cmd['r']) and !isset($cmd['s'])) {
					global $progressBar;
					$progressBar->update($proxy_id);
				}			
			});
					
			$multi_curl->start();
		
		} // End chunked proxy list
		
		
		global $goods;
		
		if (isset($cmd['o']) and $cmd['o'] != 'STDOUT')
			file_put_contents($cmd['o'], implode(PHP_EOL, $goods));
		
		if (!isset($cmd['s'])) {
			echo "\n Total: ".count($goods)." good proxy(s) were detected that match the criteria(s).\n\n";
			echo "\n About proxy levels:\n\n";
			echo "   1: Elite proxy servers hide both your IP address and the fact that you are using a proxy server at all.\n";			
			echo "   2: An anonymous proxy does not reveal your IP address but does reveal that you are using a proxy server.\n";
			echo "   3: Transparent proxies do not hide your IP Address and they don’t alter any user information.\n\n";
			global $start;
			echo " Scan completed in ".floor((microtime(true) - $start))." seconds.\n\n";
		}
	}

	/**
	 * Proxy type which provided from command prompt as prameter (in $cmd)
	 * converts CURLP supported constant.
	 *
	 * @param array $cmd Command line parameters which include proxy type (-t).	 
	 * @return string CURL proxy type constant.
	 */	
	function get_curl_proxy_type($cmd) {

		$cmd_proxy_type = strtoupper($cmd['t']);
		if ($cmd_proxy_type == 'HTTP' or $cmd_proxy_type == 'HTTPS') 
			return CURLPROXY_HTTP;		
		else if ($cmd_proxy_type == 'SOCKS4')
			return CURLPROXY_SOCKS4;
		else if ($cmd_proxy_type == 'SOCKS5')
			return CURLPROXY_SOCKS5;		
	}

	/**
	 * Access test over specified proxy. 
	 * Some proxies filter specific URLS also inject ad/suspicious codes to content.
	 * And some end points blocks connection coming from banned proxy.
	 *
	 * @param string $proxy IP:PORT
	 * @param array $cmd Command line params include test URL.
	 * @return string Yes: Access granted, No: Access not grented.
	 */	
	function blocking_test($proxy, &$cmd) {
		$proxy_type = $cmd['t'];
		$test_url = $cmd['a'];
				
		$multi_curl = new MultiCurl();
		$multi_curl->setUserAgent(random_user_agent());
		$multi_curl->setConnectTimeout($cmd['time_out']);	
		$multi_curl->setOpt(CURLOPT_TIMEOUT, $cmd['time_out']);			
		$multi_curl->setOpt(CURLOPT_RETURNTRANSFER, 1);
		$multi_curl->setOpt(CURLOPT_FOLLOWLOCATION, 1);
		$multi_curl->setOpt(CURLOPT_MAXREDIRS, 5);
		$multi_curl->setOpt(CURLOPT_SSL_VERIFYHOST, 0);
		$multi_curl->setOpt(CURLOPT_SSL_VERIFYPEER, 0);
		$multi_curl->setOpt(CURLOPT_SSL_VERIFYSTATUS, 0);		
		$multi_curl->setProxyType(get_curl_proxy_type($cmd));		
		$multi_curl->setProxy($proxy);
		$multi_curl->setProxyTunnel();
		$multi_curl->addGet($test_url);		
		$multi_curl->start();
		
		if ($multi_curl->error or $multi_curl->curlErrorCode or $instance->curlError or $instance->httpError) 
		{			
			$cmd['info'] = 'Error: ' . $multi_curl->errorCode . ': ' . $multi_curl->errorMessage;
			$multi_curl->close();
			return 'Yes';
		}
		$multi_curl->close();
		return 'No';
	}
	
	/**
	 * Tries to learn about the proxy's network infrastructure and 
	 * technology by evaluating the responses which
	 * returned from the proxy via headers.
	 *
	 * @param array $judge_headers HTTP headers returned from judge host at the end point.
	 * @param string $proxy_ip_port IP address and service port number of the proxy server to be tested.
	 * @return string $public_ip Internet output ip address of the computer running this tool.
	 */	
	function get_proxy_infrastructure($judge_headers, $proxy_ip_port, $public_ip) {
				
		# Ref: https://docs.aws.amazon.com/elasticloadbalancing/latest/classic/x-forwarded-headers.html		
		$proxy_ip = substr($proxy_ip_port, 0, strpos($proxy_ip_port, ':'));		
		
		if ($judge_headers['remote_addr'] != $proxy_ip
			and (isset($judge_headers['http_x_forwarded_proto'])
					or isset($judge_headers['http_x_forwarded_for']))) {
					
				return "The proxy is using load balancer. Traffic exits from a different host.";		
				
		} else if (isset($judge_headers['http_x_forwarded_proto'])
					or isset($judge_headers['http_x_forwarded_for'])) {
				
				return "The proxy is using load balancer.";
		}
	}
	
	/**
	 * Detect proxy's anonimity levels as described below:
	 *
	 * Level-3 (Transparent): Delivers real IP to end point.
	 * Level-2 (Anonymous): End point couldnt see the real IP but knows using a proxy.
	 * Level-1 (Elit/Full Anonymous): End point couldnt see the real IP and doesnt know using a proxy.	 
	 *
	 * @param int|string $user Either an ID or a username
	 * @param PDO $pdo A valid PDO object
	 * @return User Returns User object or null if not found
	 */	
	function get_proxy_level($judge_headers, $public_ip) {
		
		/*
			Level-3: Transparent proxies do not hide 
			your IP Address and they don’t 
			alter any user information.
		*/
		# Public IP leaked in headers
		foreach($judge_headers as $header_value)
			if (strpos($header_value, $public_ip) !== false)
				# Transparent proxy
				return 3;
		/*
			Level-2: An anonymous proxy does not reveal 
			your IP address but does reveal 
			that you are using a proxy server.
		*/
		$proxy_headers = get_common_proxy_headers();
		# Search level2 proxy headers in judge headers
		foreach($judge_headers as $header_key => $header_value) {
			$header_key = str_replace('_', '-', $header_key);
			if (in_array($header_key, $proxy_headers))
				# Anonymous proxy
				return 2;
		}
		
		/*
			Elite proxy servers hide both your IP address 
			and the fact that you are using a proxy server at all.
		*/		
		# Elite proxy
		return 1;
	}
	
	/**
	 * Finds and returns user by ID or username
	 *
	 * @param int|string $user Either an ID or a username
	 * @param PDO $pdo A valid PDO object
	 * @return User Returns User object or null if not found
	 */	
	function get_judge_url($cmd) {
				
		if (isset($cmd['j']) and isset($cmd['a']))
			die("j and a cannot be used together.\n");
		
		$judge_urls['http'][] = 'http://httpheader.net/azenv.php';
		$judge_urls['http'][] = 'http://azenv.net';
		$judge_urls['http'][] = 'http://www.meow.org.uk/cgi-bin/env.pl';
		$judge_urls['http'][] = 'http://proxyjudge.biz';
		$judge_urls['http'][] = 'http://proxyjudge.us/';
		$judge_urls['http'][] = 'http://users.on.net/~emerson/env/env.pl';
		$judge_urls['http'][] = 'http://shinh.org/env.cgi';
		$judge_urls['http'][] = 'http://www3.wind.ne.jp/hassii/env.cgi';
		$judge_urls['http'][] = 'http://proxyjudge.info/azenv.php';

		$judge_urls['https'][] = 'https://httpheader.net/azenv.php';
		$judge_urls['https'][] = 'https://wfuchs.de/azenv.php';
		$judge_urls['https'][] = 'https://proxyjudge.biz';
		
		$judge_urls['socks4'] = $judge_urls['http'];
		$judge_urls['socks5'] = $judge_urls['http'];
		

		if (isset($cmd['j']))
			if (!filter_var($url, FILTER_VALIDATE_URL))
				die("\nInvalid judge URL.\n");
			else
				return $cmd['j'];
		
		$proxy_type = trim(strtolower($cmd['t']));
				
		foreach($judge_urls[$proxy_type] as $judge_url) {
		
			$curl = new Curl();
			$curl->setOpt(CURLOPT_SSL_VERIFYPEER, 0);
			$curl->setOpt(CURLOPT_SSL_VERIFYHOST, 0);
			$curl->setOpt(CURLOPT_SSL_VERIFYSTATUS, 0);		
			$curl->get($judge_url);
			
			if ($curl->error) {
				echo "\nJudge URL: '$judge_url' out of service. Trying another judge.";	
			} else {
				return $judge_url;
			}		
		}
		
		die("\n\nExiting...\nThere is no live judge URL.\nPlease provide a judge url compatible with proxy type.\n");
	}

	/**
	 * Finds and returns user by ID or username
	 *
	 * @param int|string $user Either an ID or a username
	 * @param PDO $pdo A valid PDO object
	 * @return User Returns User object or null if not found
	 */	
	function build_proxy_list($cmd) {
		
		# Check STDIN (Standard Input)
		if (isset($cmd['stdin'])) {
			$proxy_list = array_map('trim', 
									explode("\n", 
										trim($cmd['stdin'])));
			if (count($proxy_list) == 0)
				die("\nEmpty STDIN.\n");
		
		# Check inline list
		} else if (isset($cmd['p'])) {
			$p = $cmd['p'];
			if (strpos($p, ','))
				$proxy_list = explode(',', $p);
			else
				if (strlen($p) >= 9)
					$proxy_list = [$p];
					else 
						die("\nNo proxy provided.\n");
		
		# Check input file 
		} else if (isset($cmd['f'])) {
				$fname = $cmd['f'];
				if (file_exists($fname))
				$proxy_list = array_map('trim', 
									explode("\n", 
										trim(file_get_contents($fname))));
				else
					die("\nFile '$fname' not exists.\n");

				if (count($proxy_list) == 0 or empty(trim($proxy_list[0])) )
					die("\nProxy list file is empty.\n");
			
		} else
			die("\nNo proxy provided.\n");
		
		return $proxy_list;
	}

	/**
	 * Gets public IP address of host which running this tool.
	 *
	 * @return Client's public IP address.
	 */	
	function get_public_ip() {
		$curl = new Curl();
		$curl->get('https://canhazip.com/');

		if ($curl->error) {
			die('Error: ' . $curl->errorCode . ': ' . $curl->errorMessage . " when getting public IP.\n");
		} else {
			return trim($curl->response);
		}
	}
	
	/**
	 * Get string beetween two string.
	 *
	 * @param string $string String expression to search on
	 * @param string $start Defines start delimiter.
	 * @param string $end Defines end delimiter.
	 * @return Searched string
	 */		
	function get_string_between($string, $start, $end){
		$string = ' ' . $string;
		$ini = strpos($string, $start);
		if ($ini == 0) return '';
		$ini += strlen($start);
		$len = strpos($string, $end, $ini) - $ini;
		return substr($string, $ini, $len);
	}
	
	/**
	 * Proxied response is an azenv.php reply which has 
	 * all request header from tested proxy server. 
	 * As a hidden standard judge urls/azenv.php returns 
	 * a html output. Ex: request header presented in <pre> tags
	 *
	 * @param string $raw_response proxy host's request header return from judge URL (azenv.php).
	 * @return Headers key values builded from proxy judge.
	 */		
	function build_proxied_response_header($raw_response) {

		$proxied_response =  get_string_between($raw_response, '<pre>', '</pre>');		
		$header_lines = explode("\n", $proxied_response);
		$headers = Array();
		foreach($header_lines as $num => $line) {
			$line_arr = explode('=', $line);
			$headers[strtolower(trim($line_arr[0]))] = trim($line_arr[1]);			
		}
		return $headers;
	}
	
	/**
	 * Check prequistics of The Proxy Profiler
	 * If it failed advice and exit.
	 *
	 * @return No return.
	 */	
	function check_prequistics() {
		
		$d = false;
		$v = phpversion();		
		$pos1 = strpos($v, '.');
		$v = substr($v, 0, strpos($v, '.', $pos1 + 1));
		
		# Check PHP multibyte support
		if (!function_exists('mb_check_encoding') ) {
			echo "\n Multibyte string (mb_) library not installed !\n";
			echo " To install and enable the library run commands below:\n\n";
			echo "   sudo apt install php$v-mbstring\n";
			echo "   sudo phpenmod -v $v mbstring\n\n";
			$d = true;
		}
		
		# Check php curl library 
		if (!function_exists('curl_version') ) {
			echo "\n PHP curl library not installed !\n";
			echo " To install and enable the library run commands below:\n\n";
			echo "   sudo apt install php$v-curl\n";
			echo "   sudo phpenmod -v $v curl\n\n";
			$d = true;
		}
		
		if ($d) die();
	}

	/**
	* Converts shorthand memory notation value to bytes
	* From http://php.net/manual/en/function.ini-get.php
	*
	* @param $val Memory size shorthand notation string
	*/
	function return_bytes($val) {
		$val = trim($val);
		$last = strtolower($val[strlen($val)-1]);
		$val = substr($val, 0, -1);
		switch($last) {
			// The 'G' modifier is available since PHP 5.1.0
			case 'g':
				$val *= 1024;
			case 'm':
				$val *= 1024;
			case 'k':
				$val *= 1024;
		}
		return $val;
	}	

?>