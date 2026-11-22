import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import 'package:crypto/crypto.dart';
import 'package:http/http.dart' as http;
import 'package:package_info_plus/package_info_plus.dart';
import 'package:path_provider/path_provider.dart';

import 'package:omi/backend/http/api_result.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';
import 'package:omi/env/environment_profile.dart';
import 'package:omi/utils/logger.dart';

/// Whether the self-host Android updater applies to this build.
///
/// Only a dev-flavor Android build that was explicitly built for a public HTTPS
/// self-host (`OMI_SELF_HOSTED_REMOTE=true`) talks to the authenticated update
/// feed. Store builds, iOS, desktop, and runtime API overrides never do.
abstract final class SelfHostUpdateFeature {
  static bool get enabled => isEnabledFor(
        isAndroid: !kIsWeb && Platform.isAndroid,
        profile: Env.profile,
        selfHostedRemote: Env.selfHostedRemoteEnabled,
        hasApiBaseUrlOverride: Env.hasApiBaseUrlOverride,
        apiBaseUrl: Env.apiBaseUrl,
      );

  @visibleForTesting
  static bool isEnabledFor({
    required bool isAndroid,
    required AppEnvironmentProfile profile,
    required bool selfHostedRemote,
    required bool hasApiBaseUrlOverride,
    required String? apiBaseUrl,
  }) {
    if (!isAndroid || !selfHostedRemote || hasApiBaseUrlOverride) return false;
    if (profile != AppEnvironmentProfile.localDev) return false;
    final uri = Uri.tryParse((apiBaseUrl ?? '').trim());
    return uri != null && uri.scheme.toLowerCase() == 'https' && uri.host.isNotEmpty;
  }
}

/// Validated metadata for the latest published self-host APK
/// (`GET /v2/app/android/latest`).
@immutable
class AppUpdateInfo {
  const AppUpdateInfo({
    required this.app,
    required this.package,
    required this.versionName,
    required this.versionCode,
    required this.sha256,
    required this.sizeBytes,
  });

  /// The only download path the backend publishes. Anything else is rejected so
  /// the Firebase ID token is never sent to an unexpected location.
  static const String downloadPath = '/v2/app/android/download';

  /// Upper bound for a single APK download; protects device storage from a
  /// malformed or hostile feed.
  static const int maxSizeBytes = 512 * 1024 * 1024;

  static final RegExp _sha256Pattern = RegExp(r'^[0-9a-fA-F]{64}$');

  final String app;
  final String package;
  final String versionName;
  final int versionCode;
  final String sha256;
  final int sizeBytes;

  /// Strict parse mirroring the backend contract. Returns null when any field
  /// is missing, mistyped, or out of range.
  static AppUpdateInfo? tryParse(Object? json) {
    if (json is! Map) return null;
    String? text(String key) {
      final value = json[key];
      return value is String && value.trim().isNotEmpty ? value.trim() : null;
    }

    int? positiveInt(String key) {
      final value = json[key];
      return value is int && value > 0 ? value : null;
    }

    final app = text('app');
    final package = text('package');
    final versionName = text('versionName');
    final versionCode = positiveInt('versionCode');
    final sizeBytes = positiveInt('sizeBytes');
    final digest = text('sha256');
    final downloadUrl = text('downloadUrl');
    if (app == null || package == null || versionName == null || versionCode == null || sizeBytes == null) {
      return null;
    }
    if (digest == null || !_sha256Pattern.hasMatch(digest)) return null;
    if (sizeBytes > maxSizeBytes) return null;
    if (downloadUrl != downloadPath) return null;
    return AppUpdateInfo(
      app: app,
      package: package,
      versionName: versionName,
      versionCode: versionCode,
      sha256: digest.toLowerCase(),
      sizeBytes: sizeBytes,
    );
  }
}

enum AppUpdateCheckStatus { updateAvailable, upToDate, notPublished, failed }

@immutable
class AppUpdateCheckResult {
  const AppUpdateCheckResult({
    required this.status,
    required this.currentVersionCode,
    required this.currentVersionName,
    this.info,
    this.failure,
  });

  final AppUpdateCheckStatus status;
  final int currentVersionCode;
  final String currentVersionName;
  final AppUpdateInfo? info;

  /// Machine-readable reason when [status] is [AppUpdateCheckStatus.failed].
  final String? failure;

  bool get updateAvailable => status == AppUpdateCheckStatus.updateAvailable && info != null;
}

enum AppUpdateInstallStatus { launched, permissionRequired, rejected }

@immutable
class AppUpdateInstallResult {
  const AppUpdateInstallResult(this.status, [this.reason]);

  final AppUpdateInstallStatus status;
  final String? reason;

  /// Parses the native channel reply: `launched`, `permission_required`, or `rejected:<reason>`.
  factory AppUpdateInstallResult.fromChannel(Object? reply) {
    if (reply == 'launched') return const AppUpdateInstallResult(AppUpdateInstallStatus.launched);
    if (reply == 'permission_required') return const AppUpdateInstallResult(AppUpdateInstallStatus.permissionRequired);
    if (reply is String && reply.startsWith('rejected:')) {
      return AppUpdateInstallResult(AppUpdateInstallStatus.rejected, reply.substring('rejected:'.length));
    }
    return const AppUpdateInstallResult(AppUpdateInstallStatus.rejected, 'unexpected_reply');
  }
}

class AppUpdateException implements Exception {
  const AppUpdateException(this.reason);

  final String reason;

  @override
  String toString() => 'AppUpdateException($reason)';
}

@immutable
class InstalledBuild {
  const InstalledBuild({required this.packageName, required this.versionName, required this.versionCode});

  final String packageName;
  final String versionName;
  final int versionCode;
}

typedef MetadataFetcher = Future<ApiResult<AppUpdateInfo>> Function(String url);
typedef DownloadOpener = Future<http.StreamedResponse> Function(String url, Future<void>? abortTrigger);
typedef ApkInstaller = Future<AppUpdateInstallResult> Function(String path, int versionCode);

/// Client half of the authenticated self-host Android updater: polls the feed,
/// streams the APK while hashing it, verifies size and SHA-256, and hands a
/// verified file to the platform installer.
class AppUpdateService {
  AppUpdateService({
    MetadataFetcher? fetchMetadata,
    DownloadOpener? openDownload,
    ApkInstaller? installer,
    Future<Directory> Function()? cacheDirectory,
    Future<InstalledBuild> Function()? installedBuild,
    String Function()? apiBaseUrl,
  })  : _fetchMetadata = fetchMetadata ?? _defaultFetchMetadata,
        _openDownload = openDownload ?? _defaultOpenDownload,
        _installer = installer ?? _defaultInstaller,
        _cacheDirectory = cacheDirectory ?? getTemporaryDirectory,
        _installedBuild = installedBuild ?? _defaultInstalledBuild,
        _apiBaseUrl = apiBaseUrl ?? (() => Env.apiBaseUrl ?? '');

  static final AppUpdateService instance = AppUpdateService();

  static const String latestPath = 'v2/app/android/latest';

  /// Must match `cache-path` in the dev-flavor FileProvider paths.
  static const String updateDirectoryName = 'selfhost_updates';

  static const MethodChannel _channel = MethodChannel('com.omi/selfhost_update');

  final MetadataFetcher _fetchMetadata;
  final DownloadOpener _openDownload;
  final ApkInstaller _installer;
  final Future<Directory> Function() _cacheDirectory;
  final Future<InstalledBuild> Function() _installedBuild;
  final String Function() _apiBaseUrl;

  String _url(String path) {
    final base = _apiBaseUrl().trim();
    final normalizedBase = base.endsWith('/') ? base : '$base/';
    final normalizedPath = path.startsWith('/') ? path.substring(1) : path;
    return '$normalizedBase$normalizedPath';
  }

  /// Fetches the feed and compares it with the running build. Never throws.
  Future<AppUpdateCheckResult> checkForUpdate() async {
    InstalledBuild current;
    try {
      current = await _installedBuild();
    } catch (e) {
      Logger.debug('Self-host update: package info unavailable: $e');
      return const AppUpdateCheckResult(
        status: AppUpdateCheckStatus.failed,
        currentVersionCode: 0,
        currentVersionName: '',
        failure: 'package_info',
      );
    }

    AppUpdateCheckResult result(AppUpdateCheckStatus status, {AppUpdateInfo? info, String? failure}) =>
        AppUpdateCheckResult(
          status: status,
          currentVersionCode: current.versionCode,
          currentVersionName: current.versionName,
          info: info,
          failure: failure,
        );

    try {
      return switch (await _fetchMetadata(_url(latestPath))) {
        ApiFailure(problem: final problem) when problem.kind == ApiProblemKind.notFound =>
          result(AppUpdateCheckStatus.notPublished),
        ApiFailure(problem: final problem) =>
          result(AppUpdateCheckStatus.failed, failure: _metadataFailure(problem)),
        ApiSuccess(data: final info) when info.package != current.packageName =>
          result(AppUpdateCheckStatus.failed, failure: 'package_mismatch'),
        ApiSuccess(data: final info) => result(
            info.versionCode > current.versionCode
                ? AppUpdateCheckStatus.updateAvailable
                : AppUpdateCheckStatus.upToDate,
            info: info,
          ),
      };
    } catch (e) {
      Logger.debug('Self-host update check failed: $e');
      return result(AppUpdateCheckStatus.failed, failure: 'exception');
    }
  }

  /// Streams the APK into the app cache, hashing as it goes. The file is only
  /// published under its final name after its size and SHA-256 both match the
  /// feed; any failure removes the partial file and throws [AppUpdateException].
  Future<File> downloadApk(
    AppUpdateInfo info, {
    void Function(int received, int total)? onProgress,
    Future<void>? abortTrigger,
  }) async {
    final directory = Directory('${(await _cacheDirectory()).path}/$updateDirectoryName');
    await _resetDirectory(directory);
    final partial = File('${directory.path}/omi-update-${info.versionCode}.apk.part');
    final verified = File('${directory.path}/omi-update-${info.versionCode}.apk');

    final response = await _openDownload(_url(AppUpdateInfo.downloadPath), abortTrigger);
    if (response.statusCode != 200) {
      await _discard(response);
      throw AppUpdateException('http_${response.statusCode}');
    }
    final advertised = response.contentLength;
    if (advertised != null && advertised != info.sizeBytes) {
      await _discard(response);
      throw const AppUpdateException('size_mismatch');
    }

    final digestSink = _DigestSink();
    final hashInput = sha256.startChunkedConversion(digestSink);
    final sink = partial.openWrite();
    var received = 0;
    try {
      await for (final chunk in response.stream) {
        received += chunk.length;
        if (received > info.sizeBytes) throw const AppUpdateException('size_exceeded');
        sink.add(chunk);
        hashInput.add(chunk);
        onProgress?.call(received, info.sizeBytes);
      }
      await sink.flush();
      await sink.close();
      hashInput.close();
      if (received != info.sizeBytes) throw const AppUpdateException('size_mismatch');
      if (digestSink.value?.toString() != info.sha256) throw const AppUpdateException('checksum_mismatch');
      return await partial.rename(verified.path);
    } catch (e) {
      await _closeQuietly(sink);
      await _deleteQuietly(partial);
      if (e is AppUpdateException) rethrow;
      Logger.debug('Self-host update download failed: $e');
      throw const AppUpdateException('download_failed');
    }
  }

  /// Hands a verified APK to Android. The native side re-checks the package
  /// name, version, and signing lineage before launching the system installer.
  Future<AppUpdateInstallResult> installApk(File apk, AppUpdateInfo info) async {
    try {
      return await _installer(apk.path, info.versionCode);
    } catch (e) {
      Logger.debug('Self-host update install failed: $e');
      return const AppUpdateInstallResult(AppUpdateInstallStatus.rejected, 'channel_error');
    }
  }

  static Future<void> _resetDirectory(Directory directory) async {
    if (await directory.exists()) {
      await for (final entity in directory.list()) {
        await _deleteQuietly(entity);
      }
    } else {
      await directory.create(recursive: true);
    }
  }

  static Future<void> _discard(http.StreamedResponse response) async {
    try {
      await response.stream.listen(null, onError: (Object _, StackTrace __) {}).cancel().timeout(
            const Duration(seconds: 1),
          );
    } catch (_) {}
  }

  static Future<void> _closeQuietly(IOSink sink) async {
    try {
      await sink.close();
    } catch (_) {}
  }

  static Future<void> _deleteQuietly(FileSystemEntity entity) async {
    try {
      if (await entity.exists()) await entity.delete(recursive: true);
    } catch (_) {}
  }

  static Future<ApiResult<AppUpdateInfo>> _defaultFetchMetadata(String url) => executeApi(
        request: ApiRequest(url: url, method: 'GET'),
        decode: (body) {
          final info = AppUpdateInfo.tryParse(jsonDecode(body));
          if (info == null) throw const FormatException('invalid app update metadata');
          return info;
        },
      );

  static String _metadataFailure(ApiProblem problem) {
    if (problem.kind == ApiProblemKind.transport) return 'network';
    if (problem.kind == ApiProblemKind.decode) return 'invalid_metadata';
    final statusCode = problem.statusCode;
    if (statusCode != null) return 'http_$statusCode';
    return switch (problem.kind) {
      ApiProblemKind.authTerminal => 'auth_terminal',
      ApiProblemKind.authTransient => 'auth_transient',
      _ => 'api_${problem.kind.name}',
    };
  }

  static Future<http.StreamedResponse> _defaultOpenDownload(String url, Future<void>? abortTrigger) =>
      makeRawApiCall(url: url, method: 'GET', abortTrigger: abortTrigger, timeout: const Duration(minutes: 10));

  static Future<AppUpdateInstallResult> _defaultInstaller(String path, int versionCode) async {
    final reply = await _channel.invokeMethod<String>('install', {'path': path, 'versionCode': versionCode});
    return AppUpdateInstallResult.fromChannel(reply);
  }

  static Future<InstalledBuild> _defaultInstalledBuild() async {
    final info = await PackageInfo.fromPlatform();
    return InstalledBuild(
      packageName: info.packageName,
      versionName: info.version,
      versionCode: int.tryParse(info.buildNumber) ?? 0,
    );
  }
}

class _DigestSink implements Sink<Digest> {
  Digest? value;

  @override
  void add(Digest data) => value = data;

  @override
  void close() {}
}
