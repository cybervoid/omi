package com.friend.ios.selfhost

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.SigningInfo
import android.net.Uri
import android.provider.Settings
import androidx.core.content.FileProvider
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.File

/** Dedicated provider class so it never collides with a plugin's FileProvider entry. */
class SelfHostUpdateFileProvider : FileProvider()

/**
 * Dev-flavor installer for the self-host updater. Dart has already verified the
 * APK's size and SHA-256 against the authenticated feed; this side re-checks the
 * package name, version, and signing lineage before handing the file to the
 * system package installer, which remains the final authority.
 */
object SelfHostUpdateInstaller {
    private const val CHANNEL = "com.omi/selfhost_update"
    private const val APK_MIME = "application/vnd.android.package-archive"
    private const val UPDATE_DIR = "selfhost_updates"

    fun register(flutterEngine: FlutterEngine, activity: Activity) {
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL).setMethodCallHandler { call, result ->
            if (call.method != "install") {
                result.notImplemented()
                return@setMethodCallHandler
            }
            val reply = try {
                install(activity, call.argument<String>("path"), call.argument<Number>("versionCode")?.toLong())
            } catch (e: Exception) {
                "rejected:${e.javaClass.simpleName}"
            }
            result.success(reply)
        }
    }

    private fun install(activity: Activity, path: String?, expectedVersionCode: Long?): String {
        if (path == null || expectedVersionCode == null) return "rejected:invalid_arguments"

        val updateDir = File(activity.cacheDir, UPDATE_DIR).canonicalFile
        val apk = File(path).canonicalFile
        if (apk.parentFile != updateDir || !apk.name.endsWith(".apk") || !apk.isFile) {
            return "rejected:invalid_path"
        }

        val pm = activity.packageManager
        val archive = pm.getPackageArchiveInfo(apk.path, PackageManager.GET_SIGNING_CERTIFICATES)
            ?: return "rejected:unreadable_apk"
        if (archive.packageName != activity.packageName) return "rejected:package_mismatch"
        if (archive.longVersionCode != expectedVersionCode) return "rejected:version_mismatch"

        val installed = pm.getPackageInfo(activity.packageName, PackageManager.GET_SIGNING_CERTIFICATES)
        if (archive.longVersionCode <= installed.longVersionCode) return "rejected:not_newer"
        if (!signerLineageMatches(installed.signingInfo, archive.signingInfo)) return "rejected:signer_mismatch"

        if (!pm.canRequestPackageInstalls()) {
            activity.startActivity(
                Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${activity.packageName}"))
            )
            return "permission_required"
        }

        val uri = FileProvider.getUriForFile(activity, "${activity.packageName}.selfhost_updates", apk)
        val intent = Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, APK_MIME)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            activity.startActivity(intent)
            "launched"
        } catch (_: ActivityNotFoundException) {
            "rejected:no_installer"
        }
    }

    /**
     * Same signer set, or (single signer) the archive's rotation lineage contains
     * the installed lineage. The platform installer enforces the exact rules;
     * this only turns an inevitable install failure into a clear message.
     */
    private fun signerLineageMatches(installed: SigningInfo?, archive: SigningInfo?): Boolean {
        if (installed == null || archive == null) return false
        if (installed.hasMultipleSigners() || archive.hasMultipleSigners()) {
            return installed.hasMultipleSigners() && archive.hasMultipleSigners() &&
                installed.apkContentsSigners.toSet() == archive.apkContentsSigners.toSet()
        }
        val installedLineage = installed.signingCertificateHistory?.toSet().orEmpty()
        val archiveLineage = archive.signingCertificateHistory?.toSet().orEmpty()
        return installedLineage.isNotEmpty() && archiveLineage.containsAll(installedLineage)
    }
}
