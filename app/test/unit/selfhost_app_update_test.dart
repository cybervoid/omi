import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;

import 'package:omi/env/environment_profile.dart';
import 'package:omi/services/selfhost_update/app_update_nudge.dart';
import 'package:omi/services/selfhost_update/app_update_service.dart';

const _package = 'com.friend.ios.dev';
const _base = 'https://omi.example.test/';

List<int> _apkBytes([int length = 4096]) => List<int>.generate(length, (i) => (i * 31 + 7) % 256);

Map<String, Object?> _metadata({
  List<int>? bytes,
  int versionCode = 1000,
  String package = _package,
  Object? sha,
  Object? size,
  Object? downloadUrl = AppUpdateInfo.downloadPath,
}) {
  final apk = bytes ?? _apkBytes();
  return {
    'app': 'Omi Dev',
    'package': package,
    'versionName': '1.0.544',
    'versionCode': versionCode,
    'sha256': sha ?? sha256.convert(apk).toString(),
    'sizeBytes': size ?? apk.length,
    'downloadUrl': downloadUrl,
  };
}

http.StreamedResponse _stream(List<int> bytes, {int status = 200, int? contentLength, int chunk = 1000}) {
  final chunks = <List<int>>[
    for (var i = 0; i < bytes.length; i += chunk) bytes.sublist(i, (i + chunk).clamp(0, bytes.length))
  ];
  return http.StreamedResponse(Stream<List<int>>.fromIterable(chunks), status, contentLength: contentLength);
}

void main() {
  late Directory cache;
  final requested = <String>[];

  setUp(() async {
    cache = await Directory.systemTemp.createTemp('selfhost_update_test');
    requested.clear();
  });

  tearDown(() async {
    if (await cache.exists()) await cache.delete(recursive: true);
  });

  AppUpdateService service({
    http.Response? metadata,
    http.StreamedResponse Function()? download,
    int installedVersionCode = 992,
    ApkInstaller? installer,
  }) {
    return AppUpdateService(
      apiBaseUrl: () => _base,
      cacheDirectory: () async => cache,
      installedBuild: () async =>
          InstalledBuild(packageName: _package, versionName: '1.0.543', versionCode: installedVersionCode),
      fetchMetadata: (url) async {
        requested.add(url);
        return metadata;
      },
      openDownload: (url, _) async {
        requested.add(url);
        return download!();
      },
      installer: installer,
    );
  }

  group('feature gate', () {
    bool gate({
      bool android = true,
      AppEnvironmentProfile profile = AppEnvironmentProfile.localDev,
      bool remote = true,
      bool override = false,
      String? api = _base,
    }) =>
        SelfHostUpdateFeature.isEnabledFor(
          isAndroid: android,
          profile: profile,
          selfHostedRemote: remote,
          hasApiBaseUrlOverride: override,
          apiBaseUrl: api,
        );

    test('enabled only for an opted-in Android local_dev build on HTTPS', () {
      expect(gate(), isTrue);
      expect(gate(android: false), isFalse);
      expect(gate(remote: false), isFalse);
      expect(gate(override: true), isFalse);
      expect(gate(api: 'http://10.0.2.2:8080/'), isFalse);
      expect(gate(api: null), isFalse);
      expect(gate(profile: AppEnvironmentProfile.production), isFalse);
    });
  });

  group('metadata parsing', () {
    test('accepts the backend contract', () {
      final info = AppUpdateInfo.tryParse(_metadata());
      expect(info, isNotNull);
      expect(info!.versionCode, 1000);
      expect(info.sha256, sha256.convert(_apkBytes()).toString());
    });

    test('rejects missing, mistyped, or unsafe fields', () {
      expect(AppUpdateInfo.tryParse('nope'), isNull);
      expect(AppUpdateInfo.tryParse(_metadata(sha: 'abc')), isNull);
      expect(AppUpdateInfo.tryParse(_metadata(size: 0)), isNull);
      expect(AppUpdateInfo.tryParse(_metadata(size: true)), isNull);
      expect(AppUpdateInfo.tryParse(_metadata(size: '4096')), isNull);
      expect(AppUpdateInfo.tryParse(_metadata(size: AppUpdateInfo.maxSizeBytes + 1)), isNull);
      expect(AppUpdateInfo.tryParse(_metadata(versionCode: -1)), isNull);
      expect(AppUpdateInfo.tryParse(_metadata(downloadUrl: 'https://elsewhere.test/apk')), isNull);
      expect(AppUpdateInfo.tryParse(_metadata(downloadUrl: null)), isNull);
      expect(AppUpdateInfo.tryParse({..._metadata(), 'package': ''}), isNull);
    });
  });

  group('checkForUpdate', () {
    test('reports a newer build from the authenticated feed path', () async {
      final result = await service(metadata: http.Response(jsonEncode(_metadata()), 200)).checkForUpdate();
      expect(result.status, AppUpdateCheckStatus.updateAvailable);
      expect(result.updateAvailable, isTrue);
      expect(requested.single, '${_base}v2/app/android/latest');
    });

    test('same or older build is up to date', () async {
      final result = await service(
        metadata: http.Response(jsonEncode(_metadata(versionCode: 992)), 200),
      ).checkForUpdate();
      expect(result.status, AppUpdateCheckStatus.upToDate);
    });

    test('404 means nothing is published', () async {
      final result = await service(metadata: http.Response('{}', 404)).checkForUpdate();
      expect(result.status, AppUpdateCheckStatus.notPublished);
    });

    test('network, HTTP, invalid metadata, and foreign packages fail closed', () async {
      expect((await service().checkForUpdate()).failure, 'network');
      expect((await service(metadata: http.Response('x', 500)).checkForUpdate()).failure, 'http_500');
      expect((await service(metadata: http.Response('not json', 200)).checkForUpdate()).failure, 'exception');
      expect(
        (await service(metadata: http.Response(jsonEncode(_metadata(sha: 'bad')), 200)).checkForUpdate()).failure,
        'invalid_metadata',
      );
      final foreign = await service(
        metadata: http.Response(jsonEncode(_metadata(package: 'com.friend.ios')), 200),
      ).checkForUpdate();
      expect(foreign.failure, 'package_mismatch');
      expect(foreign.updateAvailable, isFalse);
    });
  });

  group('downloadApk', () {
    final bytes = _apkBytes();
    final info = AppUpdateInfo.tryParse(_metadata(bytes: bytes))!;

    Future<List<FileSystemEntity>> updateFiles() =>
        Directory('${cache.path}/${AppUpdateService.updateDirectoryName}').list().toList();

    test('streams, verifies, and publishes the APK; reports progress', () async {
      final progress = <int>[];
      final file = await service(download: () => _stream(bytes, contentLength: bytes.length)).downloadApk(
        info,
        onProgress: (received, total) {
          expect(total, bytes.length);
          progress.add(received);
        },
      );
      expect(await file.readAsBytes(), bytes);
      expect(file.path.endsWith('omi-update-1000.apk'), isTrue);
      expect(progress.last, bytes.length);
      expect(requested.single, '${_base}v2/app/android/download');
      expect((await updateFiles()).length, 1);
    });

    test('removes stale downloads before starting', () async {
      final dir = Directory('${cache.path}/${AppUpdateService.updateDirectoryName}')..createSync(recursive: true);
      File('${dir.path}/omi-update-999.apk').writeAsBytesSync([1, 2, 3]);
      await service(download: () => _stream(bytes)).downloadApk(info);
      final names = (await updateFiles()).map((e) => e.uri.pathSegments.last).toList();
      expect(names, ['omi-update-1000.apk']);
    });

    Future<void> expectFailure(String reason, http.StreamedResponse Function() download) async {
      await expectLater(
        service(download: download).downloadApk(info),
        throwsA(isA<AppUpdateException>().having((e) => e.reason, 'reason', reason)),
      );
      expect(await updateFiles(), isEmpty, reason: 'no partial or unverified file may remain');
    }

    test('checksum mismatch is discarded', () async {
      final tampered = [...bytes]..[100] ^= 0xFF;
      await expectFailure('checksum_mismatch', () => _stream(tampered));
    });

    test('oversized body aborts mid-stream', () async {
      await expectFailure('size_exceeded', () => _stream([...bytes, ...bytes]));
    });

    test('short body is discarded', () async {
      await expectFailure('size_mismatch', () => _stream(bytes.sublist(0, 100)));
    });

    test('advertised length mismatch fails before writing', () async {
      await expectFailure('size_mismatch', () => _stream(bytes, contentLength: bytes.length + 1));
    });

    test('HTTP errors fail closed', () async {
      await expectFailure('http_401', () => _stream(const [], status: 401));
    });

    test('stream errors fail closed', () async {
      await expectFailure(
        'download_failed',
        () => http.StreamedResponse(Stream<List<int>>.error(const SocketException('reset')), 200),
      );
    });
  });

  group('installApk', () {
    final info = AppUpdateInfo.tryParse(_metadata())!;

    test('passes path and version to the native installer', () async {
      String? path;
      int? code;
      final result = await service(
        installer: (p, v) async {
          path = p;
          code = v;
          return AppUpdateInstallResult.fromChannel('launched');
        },
      ).installApk(File('/cache/selfhost_updates/omi-update-1000.apk'), info);
      expect(result.status, AppUpdateInstallStatus.launched);
      expect(path, '/cache/selfhost_updates/omi-update-1000.apk');
      expect(code, 1000);
    });

    test('channel replies map to outcomes', () {
      expect(
          AppUpdateInstallResult.fromChannel('permission_required').status, AppUpdateInstallStatus.permissionRequired);
      final rejected = AppUpdateInstallResult.fromChannel('rejected:signer_mismatch');
      expect(rejected.status, AppUpdateInstallStatus.rejected);
      expect(rejected.reason, 'signer_mismatch');
      expect(AppUpdateInstallResult.fromChannel(null).reason, 'unexpected_reply');
    });

    test('channel exceptions are reported, not thrown', () async {
      final result = await service(installer: (_, __) async => throw StateError('boom')).installApk(File('x'), info);
      expect(result.status, AppUpdateInstallStatus.rejected);
      expect(result.reason, 'channel_error');
    });
  });

  group('nudge bounds', () {
    const hour = 3600 * 1000;

    test('checks at most once per interval', () {
      expect(SelfHostUpdateNudge.isCheckDue(nowMs: 10 * hour, lastCheckMs: 0), isTrue);
      expect(SelfHostUpdateNudge.isCheckDue(nowMs: 10 * hour, lastCheckMs: 9 * hour), isFalse);
      expect(SelfHostUpdateNudge.isCheckDue(nowMs: 10 * hour, lastCheckMs: 4 * hour), isTrue);
      expect(SelfHostUpdateNudge.isCheckDue(nowMs: 4 * hour, lastCheckMs: 10 * hour), isTrue,
          reason: 'clock moved back');
    });

    test('announces each build once', () {
      expect(SelfHostUpdateNudge.shouldAnnounce(lastAnnouncedVersionCode: 0, availableVersionCode: 1000), isTrue);
      expect(SelfHostUpdateNudge.shouldAnnounce(lastAnnouncedVersionCode: 1000, availableVersionCode: 1000), isFalse);
      expect(SelfHostUpdateNudge.shouldAnnounce(lastAnnouncedVersionCode: 1000, availableVersionCode: 1001), isTrue);
    });
  });
}
