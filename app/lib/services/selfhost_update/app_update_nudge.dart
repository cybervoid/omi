import 'dart:async';

import 'package:flutter/foundation.dart';

import 'package:omi/app_globals.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/services/auth_service.dart';
import 'package:omi/services/notifications/notification_service.dart';
import 'package:omi/services/selfhost_update/app_update_service.dart';
import 'package:omi/services/selfhost_update/app_update_strings.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/widgets/selfhost_app_update_dialog.dart';

/// Bounded foreground update nudge for the self-host build.
///
/// Runs only while the signed-in Home shell is foregrounded (no server push,
/// no background work). It checks the feed at most once per [minCheckInterval]
/// and announces each newer build once: one in-app dialog plus one local
/// notification that stays in the shade as a reminder. Tapping the notification
/// re-checks the feed instead of trusting its stale payload.
class SelfHostUpdateNudge {
  SelfHostUpdateNudge({AppUpdateService? service, DateTime Function()? clock})
      : _service = service ?? AppUpdateService.instance,
        _clock = clock ?? DateTime.now;

  static final SelfHostUpdateNudge instance = SelfHostUpdateNudge();

  static const Duration minCheckInterval = Duration(hours: 6);
  static const int notificationId = 0x5E1F;
  static const String notificationPayloadKey = 'selfhost_update';
  static const String lastCheckKey = 'selfHostUpdateLastCheckMs';
  static const String lastAnnouncedKey = 'selfHostUpdateLastAnnouncedVersionCode';

  final AppUpdateService _service;
  final DateTime Function() _clock;
  bool _inFlight = false;

  /// True when enough time has passed since [lastCheckMs]. A clock that moved
  /// backwards also allows a check rather than suppressing it indefinitely.
  @visibleForTesting
  static bool isCheckDue({required int nowMs, required int lastCheckMs}) {
    if (lastCheckMs <= 0 || nowMs < lastCheckMs) return true;
    return nowMs - lastCheckMs >= minCheckInterval.inMilliseconds;
  }

  @visibleForTesting
  static bool shouldAnnounce({required int lastAnnouncedVersionCode, required int availableVersionCode}) =>
      availableVersionCode > lastAnnouncedVersionCode;

  Future<void> maybeCheck() async {
    if (!SelfHostUpdateFeature.enabled || _inFlight) return;
    if (!AuthService.instance.isSignedIn()) return;
    final prefs = SharedPreferencesUtil();
    final nowMs = _clock().millisecondsSinceEpoch;
    if (!isCheckDue(nowMs: nowMs, lastCheckMs: prefs.getInt(lastCheckKey))) return;

    _inFlight = true;
    try {
      // Stamp before the request so a failing feed is retried on the next
      // interval rather than on every resume.
      await prefs.saveInt(lastCheckKey, nowMs);
      final result = await _service.checkForUpdate();
      final info = result.info;
      if (!result.updateAvailable || info == null) return;
      if (!shouldAnnounce(
        lastAnnouncedVersionCode: prefs.getInt(lastAnnouncedKey),
        availableVersionCode: info.versionCode,
      )) {
        return;
      }
      await prefs.saveInt(lastAnnouncedKey, info.versionCode);

      await NotificationService.instance.createNotification(
        title: SelfHostUpdateStrings.updateAvailable,
        body: SelfHostUpdateStrings.notificationBody(info.versionName),
        notificationId: notificationId,
        payload: const {notificationPayloadKey: '1'},
      );

      final context = globalNavigatorKey.currentContext;
      if (context != null && context.mounted) {
        unawaited(showSelfHostUpdateDialog(context, info));
      }
    } catch (e) {
      Logger.debug('Self-host update nudge failed: $e');
    } finally {
      _inFlight = false;
    }
  }

  /// Removes the reminder once the installer has been launched.
  static void clearNotification() {
    try {
      NotificationService.instance.clearNotification(notificationId);
    } catch (_) {}
  }
}
