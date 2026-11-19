import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';

import 'package:font_awesome_flutter/font_awesome_flutter.dart';

import 'package:omi/app_globals.dart';
import 'package:omi/services/selfhost_update/app_update_nudge.dart';
import 'package:omi/services/selfhost_update/app_update_service.dart';
import 'package:omi/services/selfhost_update/app_update_strings.dart';
import 'package:omi/ui/ui.dart';

/// Shared download → verify → install dialog for the self-host updater. Used by
/// the Help & About row, the foreground nudge, and the notification tap.
Future<void> showSelfHostUpdateDialog(BuildContext context, AppUpdateInfo info, {AppUpdateService? service}) {
  return showDialog<void>(
    context: context,
    barrierDismissible: false,
    builder: (_) => SelfHostUpdateDialog(info: info, service: service ?? AppUpdateService.instance),
  );
}

/// Re-checks the feed (a notification may be hours old) and opens the dialog,
/// or reports the current state when [reportWhenNoUpdate] is set.
Future<void> openSelfHostUpdateFlow({bool reportWhenNoUpdate = true}) async {
  if (!SelfHostUpdateFeature.enabled) return;
  final result = await AppUpdateService.instance.checkForUpdate();
  final context = globalNavigatorKey.currentContext;
  if (context == null || !context.mounted) return;
  if (result.updateAvailable) {
    await showSelfHostUpdateDialog(context, result.info!);
  } else if (reportWhenNoUpdate) {
    await showOmiAlert(context, title: SelfHostUpdateStrings.checkForUpdates, message: _statusMessage(result));
  }
}

String _statusMessage(AppUpdateCheckResult result) {
  switch (result.status) {
    case AppUpdateCheckStatus.upToDate:
      return '${SelfHostUpdateStrings.upToDate}\n'
          '${SelfHostUpdateStrings.currentBuild(result.currentVersionName, result.currentVersionCode)}';
    case AppUpdateCheckStatus.notPublished:
      return SelfHostUpdateStrings.notPublished;
    case AppUpdateCheckStatus.failed:
      final reason = result.failure;
      return reason == 'package_mismatch' ? SelfHostUpdateStrings.failure(reason!) : SelfHostUpdateStrings.checkFailed;
    case AppUpdateCheckStatus.updateAvailable:
      return SelfHostUpdateStrings.updateAvailable;
  }
}

enum _Phase { idle, downloading, installing, permissionRequired, failed }

class SelfHostUpdateDialog extends StatefulWidget {
  const SelfHostUpdateDialog({super.key, required this.info, required this.service});

  final AppUpdateInfo info;
  final AppUpdateService service;

  @override
  State<SelfHostUpdateDialog> createState() => _SelfHostUpdateDialogState();
}

class _SelfHostUpdateDialogState extends State<SelfHostUpdateDialog> {
  _Phase _phase = _Phase.idle;
  double _progress = 0;
  String? _failure;
  File? _verifiedApk;
  final Completer<void> _abort = Completer<void>();

  bool get _busy => _phase == _Phase.downloading || _phase == _Phase.installing;

  @override
  void dispose() {
    if (!_abort.isCompleted) _abort.complete();
    super.dispose();
  }

  Future<void> _start() async {
    setState(() {
      _phase = _Phase.downloading;
      _progress = 0;
      _failure = null;
    });
    try {
      _verifiedApk ??= await widget.service.downloadApk(
        widget.info,
        abortTrigger: _abort.future,
        onProgress: (received, total) {
          if (mounted && total > 0) setState(() => _progress = received / total);
        },
      );
    } on AppUpdateException catch (e) {
      if (mounted) {
        setState(() {
          _phase = _Phase.failed;
          _failure = e.reason;
        });
      }
      return;
    }
    await _install();
  }

  Future<void> _install() async {
    final apk = _verifiedApk;
    if (apk == null || !mounted) return;
    setState(() => _phase = _Phase.installing);
    final result = await widget.service.installApk(apk, widget.info);
    if (!mounted) return;
    switch (result.status) {
      case AppUpdateInstallStatus.launched:
        SelfHostUpdateNudge.clearNotification();
        Navigator.of(context).pop();
      case AppUpdateInstallStatus.permissionRequired:
        setState(() => _phase = _Phase.permissionRequired);
      case AppUpdateInstallStatus.rejected:
        setState(() {
          _phase = _Phase.failed;
          _failure = result.reason ?? 'rejected';
        });
    }
  }

  @override
  Widget build(BuildContext context) {
    final percent = (_progress * 100).clamp(0, 100).toStringAsFixed(0);
    final Widget? content = switch (_phase) {
      _Phase.idle => null,
      _Phase.downloading => Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            LinearProgressIndicator(value: _progress > 0 ? _progress : null),
            const SizedBox(height: 8),
            Text('${SelfHostUpdateStrings.downloading} $percent%'),
          ],
        ),
      _Phase.installing => const Text(SelfHostUpdateStrings.verifying),
      _Phase.permissionRequired => const Text(SelfHostUpdateStrings.permissionRequired),
      _Phase.failed => Text(SelfHostUpdateStrings.failure(_failure ?? 'unknown')),
    };

    return OmiAlertDialog(
      title: SelfHostUpdateStrings.updateAvailable,
      message: SelfHostUpdateStrings.availableDescription(widget.info.versionName),
      content: content,
      actions: [
        OmiDialogAction(
          key: const ValueKey('selfhost_update_later'),
          label: _phase == _Phase.failed ? SelfHostUpdateStrings.ok : SelfHostUpdateStrings.later,
          onPressed: _busy ? null : () => Navigator.of(context).pop(),
        ),
        if (_phase != _Phase.failed)
          OmiDialogAction(
            key: const ValueKey('selfhost_update_go'),
            label: _phase == _Phase.permissionRequired ? SelfHostUpdateStrings.install : SelfHostUpdateStrings.update,
            isDefault: true,
            onPressed: _busy ? null : (_phase == _Phase.permissionRequired ? _install : _start),
          ),
      ],
    );
  }
}

/// "Check for updates" row for Settings › Help & About (self-host builds only).
class SelfHostCheckForUpdatesRow extends StatefulWidget {
  const SelfHostCheckForUpdatesRow({super.key});

  @override
  State<SelfHostCheckForUpdatesRow> createState() => _SelfHostCheckForUpdatesRowState();
}

class _SelfHostCheckForUpdatesRowState extends State<SelfHostCheckForUpdatesRow> {
  bool _checking = false;

  Future<void> _check() async {
    if (_checking) return;
    setState(() => _checking = true);
    final result = await AppUpdateService.instance.checkForUpdate();
    if (!mounted) return;
    setState(() => _checking = false);
    if (result.updateAvailable) {
      await showSelfHostUpdateDialog(context, result.info!);
    } else {
      await showOmiAlert(context, title: SelfHostUpdateStrings.checkForUpdates, message: _statusMessage(result));
    }
  }

  @override
  Widget build(BuildContext context) {
    return OmiSettingsRow(
      key: const ValueKey('settings_row_selfhost_update'),
      leading: const FaIcon(FontAwesomeIcons.download),
      title: SelfHostUpdateStrings.checkForUpdates,
      value: _checking ? SelfHostUpdateStrings.checking : null,
      onTap: _checking ? null : _check,
    );
  }
}
