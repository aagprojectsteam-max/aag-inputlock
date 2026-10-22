# AAG LockLock 2.0.0-beta.5 — Ubuntu ו־Windows


Version 2.0.0-beta.4 source update: Linux lid-ignore now uses independent logind inhibition and InputLock-owned ordinary suspend protection. It requires native protection readiness; retired AAG services and AAG Power APIs are not prerequisites. See [independent lid protection and deployment limits](docs/independent-lid-protection.md). Historical deployment records describe earlier code; this source change is not a claim of physical acceptance.

Current Linux production acceptance: [beta.4 Linux qualification](docs/BETA4-QUALIFICATION.md).

**License:** MIT — see [LICENSE](LICENSE).

הפרויקט כולל שני backends נפרדים: תמיכת Ubuntu 26.04 LTS, ‏GNOME 50 ו־Wayland המוכחת, וגרסת בטא ל־Windows 11 x64. שתי המערכות חולקות ליבת הגדרות, פרוטוקול ומדיניות, אך משתמשות במנגנוני מערכת מקוריים ונפרדים.

ב־Ubuntu מנגנון `evdev` הקיים נשאר נתמך במלואו. ב־Windows נוספו Agent ללא Terminal, סמל Tray, הגדרות, Named Pipe מאובטח, hooks למקלדת ולעכבר, שירות לשינוי זמני ובר־שחזור של מדיניות המכסה, בניית EXE ותסריטי התקנה/הסרה. תכונות Windows התלויות בחומרה מסומנות **Experimental** עד שהן עוברות את [בדיקת החומרה](windows/README.md) במחשב היעד. נעילת מסך מגע עצמאית ב־Windows אינה נתמכת כרגע והתיבה מושבתת; התוכנה אינה מציגה אירועי מגע שהומרו לעכבר כהוכחה לנעילת מסך מגע.

הוראות ההתקנה והבדיקה של Windows נמצאות ב־[`windows/README.md`](windows/README.md). הוראות Ubuntu המלאות ממשיכות להלן.

> **כלל הבטיחות החשוב ביותר:** אין לבצע נעילה מלאה ראשונה לפני ש־`input-lock doctor` עובר, קיימת דרך חילוץ דרך SSH או TTY, ונבדק Auto Unlock של 30 שניות. סדר הבדיקה המדויק נמצא בחלק 8.

## חלק 1 — סיכום הארכיטקטורה

השירות `input-lock-daemon` רץ בחשבון מערכת מוגבל בשם `input-lockd`, שחבר בקבוצת `input`. המשתמש הרגיל **אינו** נוסף לקבוצה זו. בזמן שהקלט משוחרר השירות פותח רק את התקני ה־`evdev` שסווגו כמקלדת, עכבר, Touchpad או מסך מגע, ומאזין לאירועים באמצעות `selectors`/`epoll` ול־Hotplug באמצעות `udev`.

בעת נעילה השירות מפעיל `EVIOCGRAB` רק על ההתקנים והסוגים שנבחרו. לפי ממשק הקלט של Linux, המחזיק ב־grab הופך למקבל הבלעדי של אירועי ההתקן. לכן GNOME, ‏Wayland, ‏XWayland, ‏GTK, ‏Qt, ‏Chrome, ‏Brave, ‏VS Code, ‏Obsidian, ‏Anki, חלונות WinBoat ומסך מלא אינם מקבלים את האירועים — אבל השירות עצמו ממשיך לקבל אותם ויכול לזהות את קיצור השחרור.

ה־daemon אינו מעביר הקשות דרך התקן וירטואלי ואינו משנה את פריסת המקלדת. הוא מחזיק רק קבוצה זמנית של **קודי מקשים לחוצים** לצורך זיהוי שני הצירופים; הוא אינו מפענח תווים, אינו שומר היסטוריה ואינו רושם הקשות בלוג.

ה־CLI מתקשר עם השירות באמצעות Unix socket עם פרוטוקול JSON מצומצם. ה־socket נגיש לחיבור מקומי, אך השירות בודק כל לקוח באמצעות `SO_PEERCRED` ומקבל פקודות רק מ־root או מה־UID שהוגדר בהתקנה. אין `eval`, אין shell commands מהלקוח ואין פרמטר חופשי לביצוע.

סמל ה־Tray וההתראות מופעלים מתהליך משתמש נפרד. הוא יורש את D-Bus הנכון מה־user manager של systemd, ולכן שירות המערכת אינו מנחש `DISPLAY`, ‏`WAYLAND_DISPLAY` או `DBUS_SESSION_BUS_ADDRESS`. ממשק ה־Tray באנגלית וכולל נעילה/שחרור, שינוי קיצור, התעלמות מסגירת מכסה ו־About.

הפעולה `Quit` בתפריט ה־Tray מבקשת אישור, משחררת את כל התקני הקלט ומסיימת ביציאה תקינה הן את ה־daemon והן את תהליך ה־Tray. כדי להפעיל את Input Lock שוב יש להריץ `sudo systemctl start input-lock.service` ולאחריו `systemctl --user start input-lock-notifier.service`.

לאחר ההתקנה מופיע `Input Lock` בתפריט האפליקציות של GNOME. אפשר להפעיל ממנו את התוכנה ללא Terminal; אם שירות המערכת נעצר באמצעות `Quit`, Ubuntu מציג בקשת אימות גרפית ולאחריה מפעיל מחדש את השירות ואת סמל ה־Tray.

תפריט ה־Tray כולל גם את האפשרות `Start at Login`. סימון או ביטול שלה מעדכן יחד את שירות המערכת ואת שירות ה־Tray; Ubuntu מציג בקשת אימות גרפית לשינוי שירות המערכת. שירות ה־Tray מסודר אחרי `gnome-session-initialized.target`, ולכן הוא אינו מנסה ליצור חלון לפני שחיבור Wayland של GNOME מוכן. אם התצוגה עדיין אינה זמינה הוא יוצא ללא core-dump ו־systemd מנסה שוב. ביטול האפשרות אינו סוגר את התוכנה מיד—לכך מיועדת הפעולה `Quit`.

הפעולה `Settings…` פותחת חלון הגדרות מאוחד. בחלון ניתן לבחור אילו סוגי קלט ייכללו בנעילה הרגילה—מקלדת, עכבר ו־Touchpad, ומסך מגע—לשנות את הקיצור הראשי ולהחליט אם Input Lock יופעל אוטומטית בכניסה למשתמש. יש לבחור לפחות סוג קלט אחד. הבחירות נשמרות בקובץ ההעדפות ואינן אובדות בעדכון או בהפעלה מחדש.

באותו חלון ניתן להפעיל `Automatically lock after no activity` ולבחור זמן של 1–1440 דקות. כל אירוע מקלדת, עכבר, Touchpad או מסך מגע מאפס את הספירה. הטיימר פועל רק ב־Session המורשה וכשהקלט משוחרר; בסיום הזמן הוא נועל את סוגי הקלט שנבחרו באותו חלון. ביטול תיבת הסימון משבית את Auto-Lock, וברירת המחדל לאחר התקנה חדשה היא כבויה.

האפשרות `Hide cursor after no activity` מסתירה את סמן העכבר אחרי זמן של דקות ושניות שנבחר באמצעות כפתורי ‎−/+‎. תזוזה ממשית של הסמן או שינוי במצב כפתורי העכבר מחזירים אותו מיד ומתחילים את הספירה מחדש. המימוש הוא הרחבת GNOME מצומצמת המשתמשת ישירות במיקום הסמן וב־CursorTracker של Mutter, ולכן פועל באופן גלובלי ב־Wayland ואינו מושפע מפעילות Idle מדומה של RustDesk או התקני uinput. האפשרות כבויה כברירת מחדל והעדפת המשתמש נשמרת תחת `~/.config/input-lock/`. GNOME Wayland טוען התקנה או עדכון של קוד ההרחבה בכניסה הבאה למשתמש; המתקין אינו מכבה הרחבה פעילה באמצע Session.

`Ignore Lid Close (continuous, safety-monitored)` מופעל רק עם מכסה פתוח, משתמש גרפי פעיל, בקר Tray מחובר וניטור תקין. במצב תקין הוא מתעלם מסגירת המכסה ברציפות, **ללא טיימר של 2 או 4 דקות**. בזמן שהאפשרות פעילה, ה־Tray מחזיק חסימת GNOME Idle זמנית וגם פרופיל `power-saver` **זמני בלבד**, כדי להפחית קפיצות חום ולמנוע כניסה לשינה מחוסר פעילות; בביטול האפשרות פרופיל החשמל הקודם חוזר אוטומטית. ההגדרות הקבועות של GNOME אינן משתנות, ושינת חירום/שינה ידנית אינה נחסמת. במקרה חום מסוכן, סוללה קריטית או אובדן השגחה InputLock משחרר את העיכוב ומבקש השהיה רגילה. לאחר הפעלת השירות מחדש יש להפעיל את האפשרות שוב במפורש. ראו [מדיניות ההגנה המעודכנת](docs/independent-lid-protection.md).

### השוואת החלופות

| חלופה | Wayland | קיצור שחרור בזמן נעילה | Root/הרשאה | Hotplug | סיכון/מורכבות | החלטה |
|---|---:|---:|---:|---:|---|---|
| `EVIOCGRAB` עם `evdev` | כן | כן, אם אותו daemon ממשיך לקרוא | גישה ל־`input` | כן, עם udev | בינונית; grab נסגר עם ה־fd | **נבחר** |
| `keyd` | כן | אפשרי, אך דורש תצורת remap/virtual input | שירות מערכת | כן | כלי מצוין למיפוי; כבד יותר למטרת Toggle | לא נבחר |
| `interception-tools` | כן | אפשרי בצינור סינון | שירות מערכת | דורש תצורה | מורכב יותר ומעביר את כל זרם הקלט | לא נבחר |
| `libinput` | כן | ספריית סיווג/עיבוד, לא API כללי לחסימה | משתנה | כן | אינה מספקת לבדה grab בטוח | משמשת רק לאבחון |
| `uinput` | כן | כן | גישה ל־uinput | כן | מחייב proxy קבוע וטיפול מלא ב־state | לא נחוץ כאן |
| הרחבת GNOME | כן | לא לאחר grab פיזי | משתמש | מוגבל ל־Shell | תלויה בגרסת GNOME ובמצב Shell | לא מנגנון הליבה |
| input-remapper | כן | אפשרי למיפוי, לא מיועד לנעילת seat מלאה | daemon | כן | שכבה נוספת ותלויות UI | לא נבחר |
| `xinput` | לא ב־Wayland טבעי | לא אמין | משתמש | חלקי | X11/XWayland בלבד | נפסל |
| sysfs/udev disable | לעיתים | ההתקן עצמו נעלם ולכן לא יכול לשחרר | root | מסורבל | סיכון גבוה להיתקע/להשפיע על driver | נפסל |
| חסימה ב־compositor | עקרונית | תלוי ב־GNOME API שאינו קיים כ־CLI יציב | compositor | כן | תלוי מימוש וגרסה | לא זמין כפתרון כללי |

## חלק 2 — סיכונים והחלטות בטיחות

מנגנוני ההגנה הם:

1. `Ctrl+Alt+Z` מזוהה ישירות מהתקן גם כשהמקלדת grabbed.
2. `Ctrl+Alt+Shift+F12` תמיד מבצע Emergency Unlock.
3. `Ctrl+Alt+F1..F12` בזמן נעילה משחרר תחילה את כל הקלט; לאחר שחרור המקשים, לחיצה שנייה עוברת ל־TTY.
4. אפשר להגדיר Auto Unlock גלובלי או להוסיף `--timeout SECONDS` לפקודה אחת.
5. השירות מחכה לשחרור כל מקשי הקיצור לפני ביצוע Toggle. פקודת CLI ממתינה עד שתי שניות לשחרור `Enter` או לחיצה רגילה אחרת; מקש או כפתור שנשאר לחוץ לאחר מכן גורם לסירוב בטוח.
6. נעילה היא טרנזקציונית: אם grab אחד נכשל, כל ה־grabs החדשים של אותה פעולה מבוטלים.
7. סגירת תהליך משחררת `EVIOCGRAB` ברמת ה־kernel. בנוסף יש cleanup ל־SIGTERM, ‏SIGINT, ‏SIGHUP וחריגות.
8. כל Restart מתחיל כשהמקלדת והמצביע משוחררים; קובץ המצב הוא לדיווח בלבד ואינו משמש לשחזור נעילת קלט לאחר boot. `Ignore Lid Close` פועל באופן רציף **כל עוד השירות פעיל והניטור בטוח**, אך מטעמי בטיחות מתאפס תמיד למצב הרגיל בכל הפעלה מחדש של השירות או המחשב.
9. לפני Suspend וביציאה/מעבר מה־Session המורשה מתבצע שחרור. לאחר Resume המצב נשאר משוחרר.
10. התקן חדש מסווג באירוע udev וננעל אם הוא מתאים. אם אי אפשר לתפוס אותו, הוא נשאר שמיש כנתיב חילוץ והאזהרה נרשמת ב־journal.
11. משתמש אחר אינו יכול לשלוח פקודות ל־socket. כשה־Session המורשה אינו פעיל, הצירוף הראשי הגולמי אינו יוזם נעילה; קיצור החירום עדיין רשאי רק לשחרר.
12. SSH אינו עובר דרך `/dev/input` ולכן אינו מושפע.

### מגבלות שחשוב לדעת

- `EVIOCGRAB` הוא ברמת התקן, לא ברמת אפליקציה. אם node יחיד מסומן גם כמקלדת וגם כעכבר, נעילת אחד הסוגים תופסת את ה־node כולו.
- בזמן נעילת מקלדת, TTY אינו מקבל את הצירוף הראשון. השירות מפרש `Ctrl+Alt+F3` כשחרור; אחרי שחרור המקשים יש ללחוץ שוב כדי לעבור ל־TTY.
- מסך הנעילה של GNOME שייך לאותו Session פעיל. נעילת Input Lock יכולה להישאר שם; קיצורי החירום ממשיכים לעבוד.
- למחשב מרובה־מושבים (multi-seat) נדרש פיתוח נוסף. גרסה זו מיועדת ל־seat מקומי יחיד ול־UID מורשה יחיד.
- אין דרך תוכנתית “להוכיח” חומרה ספציפית בלי בדיקה על המחשב. לכן הבדיקה הראשונה חייבת להיות עם טיימר 30 שניות ודרך חילוץ נוספת.
- `Ignore Lid Close (continuous, safety-monitored)` מתעלם מסגירת המכסה ללא מגבלת זמן אוטומטית — על חשמל, על סוללה ובעומס מעבד. בעת הפעלת האפשרות נחסמת **רק כניסת GNOME למצב Idle**, ונוצרה גם החזקה זמנית של פרופיל `power-saver` להפחתת קפיצות החום. שתי ההחזקות מסתיימות עם כיבוי האפשרות או ניתוק ה־Tray, ופרופיל החשמל הקודם משוחזר ללא שינוי קבוע בהעדפת המשתמש. לא נחסמת שינה ידנית או שינת הגנה. טמפרטורה מסוכנת, סוללה קריטית או כשל ניטור/בקרה יכולים עדיין להורות על שינה בטוחה. יש לפתוח את המכסה לפני הפעלה; אחרי Restart האפשרות כבויה עד להפעלה מפורשת. אין להשאיר מחשב פועל עם מכסה סגור בתוך תיק או ללא אוורור. ראו [תיעוד הבטיחות העדכני](docs/independent-lid-protection.md).

## חלק 3 — דרישות מוקדמות

המתקין בודק ומתקין מהמאגר הרשמי של Ubuntu:

```text
python3
python3-evdev
python3-pyudev
python3-dbus
python3-gi
gir1.2-gtk-3.0
gir1.2-ayatanaappindicator3-0.1
libnotify-bin
evtest
libinput-tools
```

לפני ההתקנה מומלץ לוודא:

```bash
cat /etc/os-release
gnome-shell --version
echo "$XDG_SESSION_TYPE"
```

הפלט המתאים לפרויקט הוא Ubuntu 26.04, ‏GNOME 50 ו־`wayland`.

## חלק 4 — מבנה הקבצים

```text
input-lock/
├── README.md
├── VERSION
├── install.sh
├── update.sh
├── uninstall.sh
├── assets/
│   ├── input-lock-locked.svg
│   └── input-lock-unlocked.svg
├── config/input-lock.conf
├── gnome-extension/
│   ├── extension.js
│   └── metadata.json
├── src/
│   ├── input-lock
│   ├── input-lock-daemon
│   ├── input-lock-notifier
│   ├── input_lock_common.py
│   ├── input_lock_daemon.py
│   ├── input_lock_cli.py
│   ├── input_lock_notifier.py
│   └── input_lock_gnome.py
├── systemd/
│   ├── input-lock.service
│   └── input-lock-notifier.service
├── integration/input-lock-system-sleep
├── tests/
│   ├── test_common.py
│   ├── test_config.sh
│   ├── test_daemon_logic.py
│   ├── test_device_detection.sh
│   ├── test_gnome.py
│   └── test_toggle.sh
└── docs/
    ├── architecture.md
    ├── security.md
    ├── testing.md
    └── troubleshooting.md
```

מיקומי ההתקנה העיקריים:

```text
/usr/bin/input-lock
/usr/lib/input-lock/
/etc/input-lock/input-lock.conf
/usr/lib/systemd/system/input-lock.service
/usr/lib/systemd/user/input-lock-notifier.service
/usr/lib/systemd/system-sleep/input-lock
/usr/share/icons/hicolor/scalable/status/input-lock-locked.svg
/usr/share/icons/hicolor/scalable/status/input-lock-unlocked.svg
/usr/share/gnome-shell/extensions/input-lock-cursor@aag-projects-team/
/var/lib/input-lock/user-settings.json
~/.config/input-lock/cursor-settings.json
/run/input-lock/control.sock
/run/input-lock/state.json
```

## חלק 5 — תוכן מלא של הקבצים

החבילה אינה מכילה קטעי פסאודו, `TODO` או קוד חסר. כל קובצי המקור המלאים נמצאים בתיקיות המוצגות בחלק 4. כדי לעיין לפני ההתקנה:

```bash
less README.md
less src/input_lock_daemon.py
less install.sh
find . -maxdepth 3 -type f -print
```

כדי לבצע בדיקת syntax ויחידות בלי להתקין:

```bash
./tests/test_config.sh
```

## חלק 6 — התקנה צעד־אחר־צעד

1. חלץ את הארכיון והיכנס לתיקייה:

   ```bash
   cd ~/Downloads
   unzip locklock-2.0.0-beta.4.zip
   cd locklock-2.0.0-beta.4
   ```

2. הרץ את הבדיקות הלא־מסוכנות:

   ```bash
   ./tests/test_config.sh
   ```

3. התקן מתוך ה־Session של המשתמש שאליו מיועד הקיצור:

   ```bash
   sudo ./install.sh
   ```

   אם הרצת כ־root ישירות ולא באמצעות `sudo`, ציין משתמש:

   ```bash
   sudo ./install.sh --user USERNAME
   ```

4. ודא שהשירות עלה **משוחרר**:

   ```bash
   input-lock status
   systemctl status input-lock.service --no-pager
   input-lock list-devices
   input-lock doctor
   ```

המתקין מגבה גרסה קיימת, שומר קובץ הגדרות קיים בעדכון, אינו מבצע נעילת ניסיון, ועוצר את השירות אם ההתקנה נכשלת לאחר שהחלו שינויים.

## חלק 7 — הגדרת הקיצור

הדרך הנוחה היא לחיצה ימנית על סמל `Input Lock` ב־Tray ובחירה ב־`Change Shortcut…`. החלון מקבל קיצור הכולל לפחות שני modifiers ומקש A–Z, ‏0–9 או F1–F12, בודק התנגשות, מעדכן יחד את ה־daemon ואת קיצור ה־GNOME ושומר את הבחירה לעדכונים ולהפעלות הבאות.

אפשר לבצע את אותה פעולה במסוף:

```bash
input-lock set-hotkey 'CTRL+ALT+X'
```

המתקין יוצר שני GNOME custom keybindings:

```text
Ctrl+Alt+Z             → /usr/bin/input-lock gnome-hotkey
Ctrl+Alt+Shift+F12     → /usr/bin/input-lock gnome-emergency
```

הם שכבת fallback בלבד. בזמן נעילה GNOME אינו מקבל את אירועי המקלדת, ולכן ה־daemon מזהה את אותם צירופים ישירות. מנגנון debounce מאחד את אירוע ה־daemon ואת קריאת GNOME כדי למנוע Toggle כפול.

הפקודות השקולות שמבוצעות על־ידי `input_lock_gnome.py` הן:

```bash
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings \
"['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/input-lock/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/input-lock-emergency/']"

gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/input-lock/ name "'Input Lock — toggle keyboard and mouse'"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/input-lock/ command "'/usr/bin/input-lock gnome-hotkey'"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/input-lock/ binding "'<Control><Alt>z'"

gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/input-lock-emergency/ name "'Input Lock — emergency unlock'"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/input-lock-emergency/ command "'/usr/bin/input-lock gnome-emergency'"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/input-lock-emergency/ binding "'<Control><Alt><Shift>F12'"
```

הסקריפט האמיתי משמר את כל הקיצורים הקיימים, בודק התנגשות לפני שינוי ומסיר רק את שני הנתיבים שלו.

## חלק 8 — בדיקה בטוחה ראשונית

אין לדלג על הסדר הזה.

1. ודא שיש SSH פעיל **או** ש־TTY עובד לפני נעילה:

   ```bash
   systemctl is-active ssh
   # או בדוק Ctrl+Alt+F3 וחזור עם Ctrl+Alt+F2/המקש המתאים במחשב שלך
   ```

2. הרץ Doctor. אל תמשיך אם יש `FAIL`:

   ```bash
   input-lock doctor
   ```

3. בדוק שחרור חירום כשהמערכת עדיין משוחררת:

   ```bash
   input-lock emergency-unlock
   ```

   לחץ גם `Ctrl+Alt+Shift+F12` וודא שאין שגיאה ב־journal:

   ```bash
   input-lock logs -n 50
   ```

4. בדוק תחילה רק עכבר/Touchpad עם טיימר של 5 שניות; המקלדת נשארת פעילה:

   ```bash
   ./tests/test_toggle.sh --armed
   ```

5. הבדיקה המלאה הראשונה — ורק עכשיו — עם שחרור אוטומטי של 30 שניות:

   ```bash
   input-lock lock all --timeout 30
   ```

6. בזמן הנעילה נסה `Ctrl+Alt+Z`. אם לא השתחרר, נסה `Ctrl+Alt+Shift+F12`. אם גם זה לא השתחרר, אל תיגע: הטיימר אמור לשחרר לאחר 30 שניות.

7. אחרי השחרור:

   ```bash
   input-lock status
   input-lock logs -n 100
   ```

רק לאחר שהוכחו הקיצור הראשי וקיצור החירום/טיימר מותר להשאיר `AUTO_UNLOCK_SECONDS=0`.

## חלק 9 — שימוש שוטף

### תפריט ה־Tray

לחיצה על הסמל מציגה תפריט באנגלית:

- `Lock Input` / `Unlock Input` — נעילה או שחרור של מצב ברירת־המחדל.
- `Shortcut: …` — הקיצור הפעיל כעת.
- `Change Shortcut…` — לכידת קיצור חדש, בדיקת התנגשות ושמירה.
- `Ignore Lid Close (continuous, safety-monitored)` מתעלם מסגירת המכסה ללא מגבלת זמן אוטומטית — על חשמל, על סוללה ובעומס מעבד. בעת הפעלת האפשרות נחסמת **רק כניסת GNOME למצב Idle**, ונוצרה גם החזקה זמנית של פרופיל `power-saver` להפחתת קפיצות החום. שתי ההחזקות מסתיימות עם כיבוי האפשרות או ניתוק ה־Tray, ופרופיל החשמל הקודם משוחזר ללא שינוי קבוע בהעדפת המשתמש. לא נחסמת שינה ידנית או שינת הגנה. טמפרטורה מסוכנת, סוללה קריטית או כשל ניטור/בקרה יכולים עדיין להורות על שינה בטוחה. יש לפתוח את המכסה לפני הפעלה; אחרי Restart האפשרות כבויה עד להפעלה מפורשת. אין להשאיר מחשב פועל עם מכסה סגור בתוך תיק או ללא אוורור. ראו [תיעוד הבטיחות העדכני](docs/independent-lid-protection.md).
- `About` — גרסה ותיאור.

בעת הפעלת התעלמות מהמכסה מופיעה אזהרת התחממות. אפשר לשלוט גם במסוף:

```bash
input-lock lid-ignore on
input-lock lid-ignore off
```

ביטול האפשרות משחרר את תפיסת המכסה ואת ה־inhibitor ומחזיר שליטה למדיניות מערכת ההפעלה. ההעדפה נשמרת בנפרד מהמצב הפעיל: כל הפעלה מחדש מתחילה ללא עקיפת המכסה ודורשת הפעלה מפורשת. יומני שחזור ישנים נשמרים עד לאימות השחזור.

```bash
input-lock status
input-lock list-devices

input-lock lock all
input-lock unlock all
input-lock toggle all

input-lock lock keyboard
input-lock unlock keyboard
input-lock toggle keyboard

input-lock lock mouse
input-lock unlock mouse
input-lock toggle mouse

input-lock lock touchpad
input-lock unlock touchpad
input-lock toggle touchpad

input-lock lock touchscreen
input-lock unlock touchscreen
input-lock toggle touchscreen

input-lock lock all-input
input-lock unlock all-input
input-lock toggle all-input

input-lock toggle all --timeout 300
input-lock emergency-unlock
input-lock doctor
input-lock logs
```

`mouse` כולל עכבר, Touchpad ו־TrackPoint לצורך תאימות לפקודות המבוקשות; `touchpad` מאפשר לנעול רק את משטח המגע. ‏`all` כולל מקלדת + עכבר + Touchpad. ‏`all-input` מוסיף מסך מגע.

לאחר עריכת `/etc/input-lock/input-lock.conf`:

```bash
sudoedit /etc/input-lock/input-lock.conf
sudo systemctl reload input-lock.service
input-lock doctor
```

Reload תמיד משחרר את הקלט לפני החלת התצורה החדשה. אם התצורה אינה תקינה היא נדחית והקלט נשאר משוחרר.

### החרגת מקלדת חירום

זהה נתיב יציב:

```bash
ls -l /dev/input/by-id/
sudo libinput list-devices
sudo evtest
udevadm info --query=property --name=/dev/input/eventX
```

הוסף לקובץ ההגדרות, לדוגמה:

```ini
EXCLUDE_DEVICES=/dev/input/by-id/usb-Emergency_Keyboard-event-kbd
```

או לפי Vendor/Product:

```ini
EXCLUDE_VENDOR_PRODUCT=046d:c31c
```

אחר כך בצע Reload ובדוק ב־`input-lock list-devices` שההתקן מסומן `EXCLUDED`. התקן מוחרג אינו grabbed, אך השירות רשאי לראות את קודי קיצור החירום שלו בזמן שהמערכת משוחררת; הוא אינו שומר אותם.

## חלק 10 — פתרון תקלות

פקודות האבחון הראשונות:

```bash
input-lock doctor
input-lock status --json
input-lock list-devices
systemctl status input-lock.service --no-pager
journalctl -u input-lock.service -b --no-pager -n 200
journalctl --user -u input-lock-notifier.service -b --no-pager -n 100
```

מדריך מפורט לפי תרחיש, כולל פלט צפוי, תיקון וביטול התיקון, נמצא ב־`docs/troubleshooting.md`.

### חילוץ חירום דרך SSH או TTY

נסה לפי הסדר:

```bash
sudo input-lock emergency-unlock
sudo systemctl stop input-lock.service
sudo systemctl disable input-lock.service
sudo pkill -f '/usr/lib/input-lock/input-lock-daemon'
```

`sudo input-lock emergency-unlock` מנסה תחילה IPC תקין. אם השירות אינו מגיב והפקודה רצה כ־root, היא עוצרת את השירות; סגירת ה־file descriptors משחררת את ה־kernel grabs.

## חלק 11 — עדכון והסרה מלאה

עדכון מתוך תיקיית גרסה חדשה:

```bash
sudo ./update.sh
```

העדכון משחרר את כל ההתקנים, שומר את קובץ ההגדרות הקיים, מגבה קבצים מותקנים ומפעיל מחדש במצב משוחרר.

הסרה מלאה:

```bash
sudo ./uninstall.sh
```

ההסרה משחררת לפני הכול את הקלט, מסירה את שירות המערכת ושירות המשתמש, את שני קיצורי GNOME, קוד, תיעוד, sleep hook, runtime/cache/backup ואת חשבון השירות אם נוצר על־ידי המתקין. היא אינה מסירה את חבילת Python/Ubuntu המשותפת ואינה מוחקת קיצורי GNOME אחרים.

## חלק 12 — בדיקת קבלה סופית

- [ ] `input-lock doctor` מציג 0 FAIL.
- [ ] `input-lock status` מציג Unlocked לאחר boot.
- [ ] `Ctrl+Alt+Z` נועל ומשחרר.
- [ ] `Ctrl+Alt+Shift+F12` משחרר תמיד.
- [ ] הבדיקה הראשונה השתחררה אוטומטית אחרי 30 שניות.
- [ ] לחיצה ארוכה אינה גורמת Lock/Unlock מחזורי.
- [ ] מקלדת בלבד, עכבר בלבד, `all` ו־`all-input` עובדים כמצופה.
- [ ] USB/Bluetooth חדש בזמן נעילה ננעל או מופיעה אזהרה ברורה ב־journal.
- [ ] התקן חירום מוחרג נשאר שמיש.
- [ ] `sudo systemctl stop input-lock.service` בזמן נעילה משחרר מיד.
- [ ] לאחר Restart אין נעילה משוחזרת.
- [ ] לפני Suspend מתבצע Unlock; לאחר Resume נשאר Unlocked.
- [ ] מעבר Session משחרר; משתמש אחר אינו מורשה ב־IPC.
- [ ] SSH נשאר נגיש בזמן נעילה.
- [ ] TTY נגיש באמצעות שחרור ראשון ולחיצה שנייה על `Ctrl+Alt+F3`.
- [ ] אין בלוג תוכן של הקשות — רק פעולות מצב ושמות התקנים.

## מקורות טכניים

- [Linux Kernel — Input Subsystem](https://docs.kernel.org/driver-api/input.html)
- [systemd — org.freedesktop.login1](https://www.freedesktop.org/software/systemd/man/org.freedesktop.login1.html)
- [systemd — resource control and DeviceAllow](https://www.freedesktop.org/software/systemd/man/systemd.resource-control.html)
- [Ubuntu 26.04 package archive](https://packages.ubuntu.com/resolute/)
