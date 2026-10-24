package jp.katsunori.notifyforwarder

import android.app.Notification
import android.content.Context
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.telecom.TelecomManager
import android.telephony.SmsManager
import android.util.Log
import java.util.Properties
import javax.mail.Authenticator
import javax.mail.Message
import javax.mail.PasswordAuthentication
import javax.mail.Session
import javax.mail.Transport
import javax.mail.internet.InternetAddress
import javax.mail.internet.MimeMessage

/**
 * 他アプリの通知を検知して、設定した宛先(SMS/メール)へ転送するサービス。
 * 「設定 > アプリ > 特別なアプリアクセス > 通知へのアクセス」でこのアプリを
 * 有効にしないと動作しない。
 */
class NotificationForwarderService : NotificationListenerService() {

    companion object {
        private const val TAG = "NotifyForwarder"
        const val PREFS_NAME = "notify_forwarder_prefs"
        const val KEY_PHONE_NUMBER = "phone_number"
        const val KEY_EXCLUDED_PACKAGES = "excluded_packages"

        // 送信方法: "sms" / "email" / "both"
        const val KEY_SEND_METHOD = "send_method"
        const val METHOD_SMS = "sms"
        const val METHOD_EMAIL = "email"
        const val METHOD_BOTH = "both"

        // メール送信用の設定(Gmail SMTP)
        const val KEY_EMAIL_FROM = "email_from"
        const val KEY_EMAIL_APP_PASSWORD = "email_app_password"
        const val KEY_EMAIL_TO = "email_to"

        // 同じアプリからの通知は、この間隔(ミリ秒)に1回までしか送らない
        private const val DEBOUNCE_MS = 30_000L
    }

    // アプリごとの最終送信時刻を保持(サービスが生きている間のみ有効)
    private val lastSentTime = mutableMapOf<String, Long>()

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        super.onNotificationPosted(sbn)

        val packageName = sbn.packageName

        // 自分自身の通知は無視
        if (packageName == applicationContext.packageName) return

        val prefs: SharedPreferences = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

        // 除外アプリのチェック
        val excluded = prefs.getStringSet(KEY_EXCLUDED_PACKAGES, emptySet()) ?: emptySet()
        if (excluded.contains(packageName)) return

        // グループ通知のサマリーなど、実質的に意味のない通知は無視
        if (sbn.notification.flags and Notification.FLAG_GROUP_SUMMARY != 0) return

        // 連続通知の抑制(デバウンス)
        val now = System.currentTimeMillis()
        val last = lastSentTime[packageName] ?: 0L
        if (now - last < DEBOUNCE_MS) return
        lastSentTime[packageName] = now

        val message = if (isPhoneApp(packageName)) {
            // 標準の電話アプリ(ダイヤラ)からの通知は専用メッセージにする
            "電話が来ました"
        } else {
            val appName = getAppLabel(packageName)
            "${appName}に通知が届きました"
        }

        val method = prefs.getString(KEY_SEND_METHOD, METHOD_SMS) ?: METHOD_SMS

        if (method == METHOD_SMS || method == METHOD_BOTH) {
            val phoneNumber = prefs.getString(KEY_PHONE_NUMBER, null)
            if (phoneNumber.isNullOrBlank()) {
                Log.w(TAG, "電話番号が未設定のためSMS送信をスキップしました")
            } else {
                sendSms(phoneNumber, message)
            }
        }

        if (method == METHOD_EMAIL || method == METHOD_BOTH) {
            val fromAddress = prefs.getString(KEY_EMAIL_FROM, null)
            val appPassword = prefs.getString(KEY_EMAIL_APP_PASSWORD, null)
            val toAddress = prefs.getString(KEY_EMAIL_TO, null)
            if (fromAddress.isNullOrBlank() || appPassword.isNullOrBlank() || toAddress.isNullOrBlank()) {
                Log.w(TAG, "メール設定が未入力のためメール送信をスキップしました")
            } else {
                sendEmail(fromAddress, appPassword, toAddress, "通知転送: $message", message)
            }
        }
    }

    /** パッケージ名から画面に表示されるアプリ名(例: LINE)を取得する */
    private fun getAppLabel(packageName: String): String {
        return try {
            val pm: PackageManager = applicationContext.packageManager
            val appInfo = pm.getApplicationInfo(packageName, 0)
            pm.getApplicationLabel(appInfo).toString()
        } catch (e: PackageManager.NameNotFoundException) {
            packageName
        }
    }

    /** 端末で設定されている標準の電話アプリ(デフォルトダイヤラ)かどうかを判定する */
    private fun isPhoneApp(packageName: String): Boolean {
        return try {
            val telecomManager = getSystemService(Context.TELECOM_SERVICE) as? TelecomManager
            val defaultDialerPackage = telecomManager?.defaultDialerPackage
            packageName == defaultDialerPackage
        } catch (e: Exception) {
            Log.w(TAG, "標準電話アプリの判定に失敗しました", e)
            false
        }
    }

    private fun sendSms(phoneNumber: String, message: String) {
        try {
            @Suppress("DEPRECATION")
            val smsManager = SmsManager.getDefault()
            // 長文になった場合に自動で分割して送信する
            val parts = smsManager.divideMessage(message)
            smsManager.sendMultipartTextMessage(phoneNumber, null, parts, null, null)
            Log.d(TAG, "SMS送信しました: $message -> $phoneNumber")
        } catch (e: Exception) {
            Log.e(TAG, "SMS送信に失敗しました", e)
        }
    }

    /**
     * Gmail SMTP経由でメールを送信する。
     * ネットワーク通信はメインスレッドで行えないため、別スレッドで実行する。
     */
    private fun sendEmail(
        fromAddress: String,
        appPassword: String,
        toAddress: String,
        subject: String,
        body: String
    ) {
        Thread {
            try {
                val props = Properties().apply {
                    put("mail.smtp.host", "smtp.gmail.com")
                    put("mail.smtp.port", "587")
                    put("mail.smtp.auth", "true")
                    put("mail.smtp.starttls.enable", "true")
                }

                val session = Session.getInstance(props, object : Authenticator() {
                    override fun getPasswordAuthentication(): PasswordAuthentication {
                        return PasswordAuthentication(fromAddress, appPassword)
                    }
                })

                val mimeMessage = MimeMessage(session).apply {
                    setFrom(InternetAddress(fromAddress))
                    setRecipients(Message.RecipientType.TO, InternetAddress.parse(toAddress))
                    setSubject(subject)
                    setText(body)
                }

                Transport.send(mimeMessage)
                Log.d(TAG, "メール送信しました: $subject -> $toAddress")
            } catch (e: Exception) {
                Log.e(TAG, "メール送信に失敗しました", e)
            }
        }.start()
    }
}
