import 'package:flutter/material.dart';

import 'package:omi/app_globals.dart';
import 'package:omi/services/app_update_service.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Shared "update available" dialog (download progress + sideload install) for
/// the self-host in-app updater. Used by the About page, the foreground update
/// nudge, and the update notification tap so they all share one flow.
Future<void> showAppUpdateDialog(BuildContext context, AppUpdateInfo info) async {
  double progress = 0;
  bool downloading = false;
  await showDialog(
    context: context,
    barrierDismissible: false,
    builder: (ctx) {
      return StatefulBuilder(
        builder: (ctx, setDialogState) {
          return AlertDialog(
            title: Text(context.l10n.updateAvailable),
            content: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(context.l10n.updateAvailableDescription(info.versionName)),
                if (downloading) ...[
                  const SizedBox(height: 16),
                  LinearProgressIndicator(value: progress > 0 ? progress : null),
                  const SizedBox(height: 8),
                  Text('${(progress * 100).clamp(0, 100).toStringAsFixed(0)}%'),
                ],
              ],
            ),
            actions: [
              TextButton(onPressed: downloading ? null : () => Navigator.pop(ctx), child: Text(context.l10n.cancel)),
              TextButton(
                onPressed: downloading
                    ? null
                    : () async {
                        setDialogState(() => downloading = true);
                        try {
                          final file = await AppUpdateService.instance.downloadApk(
                            info,
                            onProgress: (received, total) {
                              if (total > 0) setDialogState(() => progress = received / total);
                            },
                          );
                          final ok = await AppUpdateService.instance.installApk(file);
                          if (ctx.mounted) Navigator.pop(ctx);
                          if (!ok && context.mounted) {
                            _showInfoDialog(context, context.l10n.updateAvailable, context.l10n.somethingWentWrong);
                          }
                        } catch (_) {
                          if (ctx.mounted) Navigator.pop(ctx);
                          if (context.mounted) {
                            _showInfoDialog(context, context.l10n.updateAvailable, context.l10n.somethingWentWrong);
                          }
                        }
                      },
                child: Text(context.l10n.update),
              ),
            ],
          );
        },
      );
    },
  );
}

void _showInfoDialog(BuildContext context, String title, String message) {
  showDialog(
    context: context,
    builder: (ctx) => AlertDialog(
      title: Text(title),
      content: Text(message),
      actions: [TextButton(onPressed: () => Navigator.pop(ctx), child: Text(context.l10n.ok))],
    ),
  );
}

/// Re-checks the feed and, if a newer build is available, shows the update
/// dialog. Invoked when the user taps the "update available" notification
/// (which may be long after it was posted, so we re-check rather than trust a
/// stale payload).
Future<void> openAppUpdateFlow() async {
  final result = await AppUpdateService.instance.checkForUpdate();
  final ctx = globalNavigatorKey.currentContext;
  if (ctx == null) return;
  if (result.updateAvailable && result.info != null) {
    await showAppUpdateDialog(ctx, result.info!);
  } else if (result.error == null) {
    _showInfoDialog(ctx, ctx.l10n.checkForUpdates, ctx.l10n.appUpToDate);
  }
}
