# awscli S3 Homebrew Tap

Formula: `awscli-s3`. Installed command: `awscli`.
Requires the official AWS CLI (`aws`), Python and Git.

Install on macOS or Linux with Homebrew:

```sh
brew install CabelChen/awscli-s3/awscli-s3
awscli --help
awscli zsh
```

Configuration remains at `~/.config/s3-agent/config.ini`.
Installation does not write credentials or overwrite user configuration.

To configure a new client:

```sh
mkdir -p ~/.config/s3-agent
cp -n "$(brew --prefix awscli-s3)/libexec/config.example.ini" ~/.config/s3-agent/config.ini
```

Edit the S3 prefix, agent name and optional AWS profile in config.ini.
Set up AWS credentials on this computer using the official `aws` command.
For Bash/Zsh/Git, set the server's absolute `git_repo` path or use `--repo`.
The server agent must already be running against the same S3 prefix.

```sh
awscli doctor
awscli zsh
```

Bash/Zsh execute each line as a separate remote task. Variables do not
persist between lines; interactive TTY applications are unsupported.
MCP integration is available through `awscli-mcp`.

To run this installation as the server agent:

```sh
awscli agent --name server-a --work-dir "$HOME/.local/state/awscli-agent"
```

The agent reads the `[client]` S3 settings from the same config.ini.
Configure AWS authentication on the server, and use a distinct agent name
for each computer sharing a S3 prefix. This starts a foreground process.
Logs and task state are kept in the work directory, with seven-day retention.

Release archives use the names `awscli-0.4.1.tar.gz` (full tool) and
`awscli-agent-0.4.1.tar.gz` (agent-only installer). Both expose
`awscli agent`; the agent-only installer preserves an existing full awscli.
