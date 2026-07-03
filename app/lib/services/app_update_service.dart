import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:open_filex/open_filex.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/logger.dart';

/// Metadata for an available self-host app build, mirrored from the backend
/// feed (`GET /v2/app/android/latest`, backed by the CI-generated
/// `latest.json`). Only used by the self-hosted dev flavor.
class AppUpdateInfo {
  final String app;
  final String package;
  final String versionName;
  final int versionCode;
  final String sha256;
  final int sizeBytes;
  final String downloadUrl;
  final String? generatedAt;

  AppUpdateInfo({
    required this.app,
    required this.package,
    required this.versionName,
    required this.versionCode,
    required this.sha256,
    required this.sizeBytes,
    required this.downloadUrl,
    this.generatedAt,
  });

  factory AppUpdateInfo.fromJson(Map<String, dynamic> json) {
    int asInt(dynamic v) => (v is int) ? v : int.tryParse('$v') ?? 0;
    return AppUpdateInfo(
      app: json['app']?.toString() ?? 'Omi',
      package: json['package']?.toString() ?? '',
      versionName: json['versionName']?.toString() ?? '',
      versionCode: asInt(json['versionCode']),
      sha256: json['sha256']?.toString() ?? '',
      sizeBytes: asInt(json['sizeBytes']),
      downloadUrl: json['downloadUrl']?.toString() ?? '',
      generatedAt: json['generatedAt']?.toString(),
    );
  }
}

/// Outcome of a manual "Check for updates" run.
class AppUpdateCheckResult {
  final AppUpdateInfo? info;
  final int currentVersionCode;
  final String currentVersionName;
  final bool updateAvailable;

  /// Non-null when the check could not complete (network / server error).
  final String? error;

  AppUpdateCheckResult({
    this.info,
    required this.currentVersionCode,
    required this.currentVersionName,
    required this.updateAvailable,
    this.error,
  });
}

/// Accumulates the final [Digest] from a chunked SHA-256 conversion so we can
/// hash the APK stream without buffering the whole file in memory.
class _DigestSink implements Sink<Digest> {
  Digest? value;

  @override
  void add(Digest data) => value = data;

  @override
  void close() {}
}

/// Self-host in-app updater. Talks to the backend-mediated, Firebase-auth'd
/// update feed (no embedded secrets) and sideloads a signed APK on Android.
class AppUpdateService {
  AppUpdateService._();

  static final AppUpdateService instance = AppUpdateService._();

  static const String _latestPath = 'v2/app/android/latest';
  static const String _downloadPath = 'v2/app/android/download';

  String get _baseUrl => Env.apiBaseUrl ?? '';

  /// Fetches the latest build metadata and compares it to the running build.
  /// Never throws — failures are reported via [AppUpdateCheckResult.error].
  Future<AppUpdateCheckResult> checkForUpdate() async {
    final packageInfo = await PackageInfo.fromPlatform();
    final currentCode = int.tryParse(packageInfo.buildNumber) ?? 0;

    AppUpdateCheckResult failure(String error) => AppUpdateCheckResult(
          currentVersionCode: currentCode,
          currentVersionName: packageInfo.version,
          updateAvailable: false,
          error: error,
        );

    try {
      final response = await makeApiCall(
        url: '$_baseUrl$_latestPath',
        headers: {},
        body: '',
        method: 'GET',
      );

      if (response == null) return failure('network');

      // No feed has been published yet — treat as "up to date".
      if (response.statusCode == 404) {
        return AppUpdateCheckResult(
          currentVersionCode: currentCode,
          currentVersionName: packageInfo.version,
          updateAvailable: false,
        );
      }
      if (response.statusCode != 200) return failure('http_${response.statusCode}');

      final info = AppUpdateInfo.fromJson(jsonDecode(response.body) as Map<String, dynamic>);
      return AppUpdateCheckResult(
        info: info,
        currentVersionCode: currentCode,
        currentVersionName: packageInfo.version,
        updateAvailable: info.versionCode > currentCode,
      );
    } catch (e, stackTrace) {
      Logger.debug('App update check failed: $e $stackTrace');
      return failure('exception');
    }
  }

  String _resolveDownloadUrl(AppUpdateInfo info) {
    final url = info.downloadUrl;
    if (url.startsWith('http')) return url;
    if (url.isEmpty) return '$_baseUrl$_downloadPath';
    final path = url.startsWith('/') ? url.substring(1) : url;
    return '$_baseUrl$path';
  }

  /// Streams the APK to a cache file, reporting progress and verifying the
  /// SHA-256 against the feed metadata. Returns the downloaded file.
  /// Throws on HTTP error or checksum mismatch.
  Future<File> downloadApk(
    AppUpdateInfo info, {
    void Function(int received, int total)? onProgress,
  }) async {
    final streamed = await makeRawApiCall(url: _resolveDownloadUrl(info), method: 'GET');
    if (streamed.statusCode != 200) {
      throw Exception('Download failed: HTTP ${streamed.statusCode}');
    }

    final dir = await getTemporaryDirectory();
    // The cache directory may not exist yet on a fresh install; create it
    // before opening the target file for write.
    await dir.create(recursive: true);
    final file = File(p.join(dir.path, 'omi-dev-update-${info.versionCode}.apk'));
    if (await file.exists()) await file.delete();

    final total = streamed.contentLength ?? info.sizeBytes;
    final sink = file.openWrite();
    final digestSink = _DigestSink();
    final hashInput = sha256.startChunkedConversion(digestSink);
    int received = 0;
    try {
      await for (final chunk in streamed.stream) {
        sink.add(chunk);
        hashInput.add(chunk);
        received += chunk.length;
        if (onProgress != null && total > 0) onProgress(received, total);
      }
    } finally {
      await sink.close();
      hashInput.close();
    }

    if (info.sha256.isNotEmpty) {
      final actual = digestSink.value?.toString() ?? '';
      if (actual.toLowerCase() != info.sha256.toLowerCase()) {
        await file.delete();
        throw Exception('Checksum mismatch: expected ${info.sha256}, got $actual');
      }
    }

    return file;
  }

  /// Opens the downloaded APK so Android's package installer takes over.
  /// The user still approves the sideload. Returns true if the installer
  /// launched.
  Future<bool> installApk(File apk) async {
    final result = await OpenFilex.open(apk.path, type: 'application/vnd.android.package-archive');
    Logger.debug('open_filex result: ${result.type} ${result.message}');
    return result.type == ResultType.done;
  }
}
