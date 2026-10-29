# 系统级 unit（生产真源）

这些文件由 root 手工部署到 `/etc/systemd/system/`，**不经过 `make timer-install`**
（那条只装 user 级白名单，见上级目录 `systemd/` 与 Makefile `USER_TIMERS`）。

改这里的文件后须：

    sudo cp systemd/system/<unit> /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl restart <unit>   # 若在跑

与 `/etc` 的差异检查：

    for f in systemd/system/*.service systemd/system/*.timer; do
      b=$(basename "$f")
      sudo diff -u "$f" "/etc/systemd/system/$b" || true
    done
