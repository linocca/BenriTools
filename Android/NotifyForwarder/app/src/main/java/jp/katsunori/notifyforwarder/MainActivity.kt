package jp.katsunori.notifyforwarder

import android.Manifest
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.os.Bundle
import android.provider.Settings
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import jp.katsunori.notifyforwarder.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: SharedPreferences

    private val requestSmsPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        val msg = if (granted) "SMS送信が許可されました" else "SMS送信が許可されませんでした"
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
        updatePermissionStatus()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        prefs = getSharedPreferences(
            NotificationForwarderService.PREFS_NAME, MODE_PRIVATE
        )

        loadPreferences()

        binding.buttonSave.setOnClickListener {
            savePreferences()
            Toast.makeText(this, "保存しました", Toast.LENGTH_SHORT).show()
        }

        binding.buttonOpenNotificationSettings.setOnClickListener {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }

        binding.buttonRequestSmsPermission.setOnClickListener {
            requestSmsPermission.launch(Manifest.permission.SEND_SMS)
        }

        binding.buttonExcludedApps.setOnClickListener {
            startActivity(Intent(this, ExcludedAppsActivity::class.java))
        }

        updatePermissionStatus()
    }

    override fun onResume() {
        super.onResume()
        updatePermissionStatus()
    }

    private fun loadPreferences() {
        binding.editPhoneNumber.setText(
            prefs.getString(NotificationForwarderService.KEY_PHONE_NUMBER, "")
        )
        binding.editEmailFrom.setText(
            prefs.getString(NotificationForwarderService.KEY_EMAIL_FROM, "")
        )
        binding.editEmailAppPassword.setText(
            prefs.getString(NotificationForwarderService.KEY_EMAIL_APP_PASSWORD, "")
        )
        binding.editEmailTo.setText(
            prefs.getString(NotificationForwarderService.KEY_EMAIL_TO, "")
        )

        when (prefs.getString(NotificationForwarderService.KEY_SEND_METHOD, NotificationForwarderService.METHOD_SMS)) {
            NotificationForwarderService.METHOD_EMAIL -> binding.radioMethodEmail.isChecked = true
            NotificationForwarderService.METHOD_BOTH -> binding.radioMethodBoth.isChecked = true
            else -> binding.radioMethodSms.isChecked = true
        }
    }

    private fun savePreferences() {
        val method = when (binding.radioGroupMethod.checkedRadioButtonId) {
            binding.radioMethodEmail.id -> NotificationForwarderService.METHOD_EMAIL
            binding.radioMethodBoth.id -> NotificationForwarderService.METHOD_BOTH
            else -> NotificationForwarderService.METHOD_SMS
        }

        prefs.edit()
            .putString(
                NotificationForwarderService.KEY_PHONE_NUMBER,
                binding.editPhoneNumber.text.toString().trim()
            )
            .putString(
                NotificationForwarderService.KEY_EMAIL_FROM,
                binding.editEmailFrom.text.toString().trim()
            )
            .putString(
                NotificationForwarderService.KEY_EMAIL_APP_PASSWORD,
                binding.editEmailAppPassword.text.toString().trim()
            )
            .putString(
                NotificationForwarderService.KEY_EMAIL_TO,
                binding.editEmailTo.text.toString().trim()
            )
            .putString(NotificationForwarderService.KEY_SEND_METHOD, method)
            .apply()
    }

    private fun updatePermissionStatus() {
        val smsGranted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.SEND_SMS
        ) == PackageManager.PERMISSION_GRANTED

        val notificationAccessGranted = isNotificationServiceEnabled()

        binding.textStatus.text = buildString {
            append("SMS送信の許可: ")
            append(if (smsGranted) "OK" else "未許可(ボタンを押してください)")
            append("\n通知へのアクセス: ")
            append(if (notificationAccessGranted) "OK" else "未許可(ボタンを押して有効化してください)")
        }
    }

    private fun isNotificationServiceEnabled(): Boolean {
        val enabledListeners = Settings.Secure.getString(
            contentResolver, "enabled_notification_listeners"
        ) ?: return false
        return enabledListeners.contains(packageName)
    }
}
