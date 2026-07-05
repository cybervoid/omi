import 'package:omi/app_globals.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/flavors.dart';
import 'package:omi/services/app_update_service.dart';
import 'package:omi/services/auth_service.dart';
import 'package:omi/services/notifications.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/widgets/app_update_dialog.dart';

/// Foreground update nudge for the self-hosted dev build. On app launch and on
/// resume it checks the self-host feed and, if a newer build is available,
/// posts a local notification and shows the in-app update dialog — once per new
/// build (throttled), so it never nags on every resume. There is no server
/// push; the check only runs while the app is foregrounded (client-only, by
/// design).
class AppUpdateNudge {
  AppUpdateNudge._();

  static final AppUpdateNudge instance = AppUpdateNudge._();

  bool _inFlight = false;

  Future<void> maybeCheckAndNudge() async {
    // Self-host dev flavor only — the feed + sideload updater don't apply to prod.
    if (F.env != Environment.dev) return;
    if (_inFlight) return;
    _inFlight = true;
    try {
      // Only for signed-in users past onboarding, so we never interrupt those flows.
      if (!SharedPreferencesUtil().onboardingCompleted) return;
      if ((await AuthService.instance.getIdToken()) == null) return;

      final result = await AppUpdateService.instance.checkForUpdate();
      final info = result.info;
      if (!result.updateAvailable || info == null) return;

      // Throttle: nudge at most once per (newer) build.
      if (SharedPreferencesUtil().lastNotifiedUpdateVersionCode >= info.versionCode) return;
      SharedPreferencesUtil().lastNotifiedUpdateVersionCode = info.versionCode;

      // 1) Local system notification (tap -> update flow via NotificationUtil).
      await NotificationUtil.showAppUpdateNotification(info.versionName);

      // 2) In-app dialog when we have a live navigator context (i.e. foregrounded).
      final ctx = globalNavigatorKey.currentContext;
      if (ctx != null) await showAppUpdateDialog(ctx, info);
    } catch (e, s) {
      Logger.debug('AppUpdateNudge failed: $e $s');
    } finally {
      _inFlight = false;
    }
  }
}
