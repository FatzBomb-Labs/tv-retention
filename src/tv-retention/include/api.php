<?php
declare(strict_types=1);
// Served from Unraid's authenticated /plugins location. There is no standalone listener:
// every request arrives already authenticated by emhttp, and is re-checked against the
// session CSRF token before any work is handed to the Python worker.

const TVR_SETTINGS = '/boot/config/plugins/tv-retention/settings.json';

function fail_request(int $status, string $message): never {
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(['ok' => false, 'error' => $message]);
    exit;
}

function tvr_check_token(string $provided): void {
    $vars = @parse_ini_file('/var/local/emhttp/var.ini', false, INI_SCANNER_RAW);
    $expected = is_array($vars) ? ($vars['csrf_token'] ?? '') : '';
    if ($expected === '' || !hash_equals($expected, $provided)) {
        fail_request(403, 'Session token expired. Reload the Unraid page.');
    }
}

// -- artwork ----------------------------------------------------------------
// Sonarr already stores every poster; this streams one through, so the browser never
// needs an API key and the plugin never keeps a second copy of a 3.6 GB library. Cached
// on disk because a grid asks for fifty at once and Sonarr should be asked once.
if (($_SERVER['REQUEST_METHOD'] ?? '') === 'GET' && isset($_GET['poster'])) {
    tvr_check_token((string)($_GET['csrf_token'] ?? ''));
    $series = (int)($_GET['poster'] ?? 0);
    $wanted = (string)($_GET['instance'] ?? '');
    if ($series <= 0) fail_request(400, 'No series');
    // Instance ids are generated (12 hex characters) and this one becomes part of a
    // filename, so it is checked for shape before it is used as one — the lookup below
    // would reject anything unknown anyway, but a path is not the place to find out.
    if (!preg_match('/^[a-f0-9]{1,32}$/', $wanted)) fail_request(400, 'No such Sonarr instance');

    $settings = json_decode((string)@file_get_contents(TVR_SETTINGS), true);
    if (!is_array($settings)) fail_request(503, 'Settings are unreadable');
    $instance = null;
    foreach (($settings['instances'] ?? []) as $candidate) {
        if (($candidate['id'] ?? '') === $wanted) { $instance = $candidate; break; }
    }
    if ($instance === null) fail_request(404, 'No such Sonarr instance');

    $dir = rtrim((string)($settings['state_dir'] ?? '/mnt/user/appdata/tv-retention'), '/') . '/posters';
    // The stamp is Sonarr's own artwork path, which carries its last-write marker. Hashed
    // into the filename so a new picture is a new file: keyed on the series alone, the
    // first poster ever fetched was served for ever, and a day of browser caching on top
    // of it. Hashed rather than used directly because it arrives from a query string and
    // is about to become a path.
    $stamp = substr(md5((string)($_GET['stamp'] ?? '')), 0, 12);
    $prefix = $dir . '/' . $wanted . '-' . $series;
    $cached = $prefix . '-' . $stamp . '.jpg';
    if (!is_file($cached) || filesize($cached) === 0) {
        @mkdir($dir, 0755, true);
        // Whatever this series looked like before. Left behind, every artwork change
        // would add a file and remove none.
        foreach (glob($prefix . '-*.jpg') ?: [] as $stale) @unlink($stale);
        // 250px: eight kilobytes against sixty for the full size, and a card is smaller
        // than either. The larger ones stay in Sonarr, where they already are.
        $url = rtrim((string)$instance['url'], '/') . '/api/v3/mediacover/' . $series . '/poster-250.jpg';
        $stream = stream_context_create(['http' => [
            'method' => 'GET', 'timeout' => 15, 'ignore_errors' => true,
            'header' => 'X-Api-Key: ' . (string)$instance['api_key'],
        ]]);
        $body = @file_get_contents($url, false, $stream);
        $status = 0;
        foreach ($http_response_header ?? [] as $line) {
            if (preg_match('#^HTTP/\S+\s+(\d+)#', $line, $found)) $status = (int)$found[1];
        }
        if ($body === false || $status !== 200 || $body === '') {
            http_response_code(404);
            exit;
        }
        @file_put_contents($cached, $body);
    }
    header('Content-Type: image/jpeg');
    header('Cache-Control: private, max-age=86400');
    header('X-Content-Type-Options: nosniff');
    readfile($cached);
    exit;
}

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');

if ($_SERVER['REQUEST_METHOD'] !== 'POST') fail_request(405, 'POST required');

// Unraid's auto-prepend validates the POST token, keeps $csrf_token, then unsets the field.
tvr_check_token((string)($_POST['csrf_token'] ?? ($csrf_token ?? '')));

$payload = $_POST['payload'] ?? '';
if (!is_string($payload) || strlen($payload) > 1048576) fail_request(413, 'Request too large');
$request = json_decode($payload, true);
// The worker owns the list of actions and rejects anything it does not know. A copy of
// that list here went stale the moment three actions were added, and the interface got
// "Unknown action" for work the backend was perfectly willing to do.
if (!is_array($request) || !is_string($request['action'] ?? null)) fail_request(400, 'No action');
if (!is_executable('/usr/bin/python3')) fail_request(503, 'Python 3 is unavailable on this server.');

$command = ['/usr/bin/python3', dirname(__DIR__) . '/worker/main.py', 'rpc'];
$descriptors = [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']];
// A fixed environment: nothing from the request can redirect the worker's paths.
$env = ['PATH' => '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'LANG' => 'C.UTF-8'];
$proc = proc_open($command, $descriptors, $pipes, null, $env);
if (!is_resource($proc)) fail_request(503, 'Could not start the TV Retention backend.');

fwrite($pipes[0], $payload);
fclose($pipes[0]);
$output = stream_get_contents($pipes[1]);
fclose($pipes[1]);
$errors = stream_get_contents($pipes[2]);
fclose($pipes[2]);
proc_close($proc);

$result = json_decode($output, true);
if (!is_array($result) || !array_key_exists('ok', $result)) {
    error_log('TV Retention backend failure: ' . substr((string)$errors, 0, 2000));
    fail_request(500, 'The TV Retention backend failed. Check the system log for details.');
}
if (!$result['ok']) http_response_code(409);
echo json_encode($result, JSON_INVALID_UTF8_SUBSTITUTE);
