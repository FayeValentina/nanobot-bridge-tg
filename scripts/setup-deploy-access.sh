#!/usr/bin/env bash
# Run once on the deployment server. Never prints the private key.
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
umask 077
key_dir="$HOME/.local/share/nanobot-bridge-tg-deploy/ssh"
mkdir -p "$key_dir" "$HOME/.ssh"
if [[ ! -f "$key_dir/github-actions" ]]; then
  ssh-keygen -q -t ed25519 -N '' -C nanobot-bridge-tg-deploy -f "$key_dir/github-actions"
fi
public_key="$(cat "$key_dir/github-actions.pub")"
authorized_line="restrict,command=\"$repo_dir/scripts/bridge_receive.py\" $public_key"
touch "$HOME/.ssh/authorized_keys"
chmod 700 "$HOME/.ssh"
chmod 600 "$HOME/.ssh/authorized_keys"
if ! grep -Fqx -- "$authorized_line" "$HOME/.ssh/authorized_keys"; then
  printf '\n%s\n' "$authorized_line" >> "$HOME/.ssh/authorized_keys"
fi
printf 'Deployment-only private key saved to: %s\n' "$key_dir/github-actions"
printf 'Add its contents to the GitHub production environment secret DEPLOY_SSH_KEY.\n'
printf 'This SSH identity can only submit deployment bundles; shell/forwarding is disabled.\n'
