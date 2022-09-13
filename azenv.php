<?php	

	$headers = apache_request_headers();
	$headers['REMOTE_ADDR'] = @$_SERVER['REMOTE_ADDR'];
	$headers['REMOTE_HOST'] = @$_SERVER['REMOTE_HOST'];
	$headers['REMOTE_PORT'] = @$_SERVER['REMOTE_PORT'];
	
	// HTML output for web browsers
	if ( @$_GET['format'] == 1) 
		foreach ($headers as $key => $value)
			echo "$key: $value<br>\n";
			
	// Text output for console based browsers
	else if (@$_GET['format'] == 2)
		foreach ($headers as $key => $value)
			echo "$key: $value\n";
			
	// Beautifed JSON output
	else {
		header('Content-type: application/json');
		echo json_encode($headers, JSON_PRETTY_PRINT|JSON_UNESCAPED_SLASHES);		
	}
		

/* 
	Other public HTTP judges
	------------------
	
	azenv Url: http://httpheader.net/azenv.php
	for SSL https://httpheader.net/azenv.php
	
	Same services:
	http://azenv.net
	http://wfuchs.de/azenv.php
	http://www.meow.org.uk/cgi-bin/env.pl
	http://www.proxyjudge.biz/
	http://httpheader.net/
	http://52.27.208.157/azenv.php
	http://www.sbjudge3.com/azenv.php
	http://54.244.185.141/azenv2.php
	http://proxyjudge.us/
	http://www2t.biglobe.ne.jp/~take52/test/env.cgi
	http://users.on.net/~emerson/env/env.pl
	http://shinh.org/env.cgi
	http://www.sbjudge4.com/azenv.php
	http://www.9ravens.com/env.cgi
	http://www3.wind.ne.jp/hassii/env.cgi
	https://aranguren.org/azenv.php
	http://proxyjudge.info/azenv.php
	http://www.sbjudge3.com/azenv.php
*/
?>