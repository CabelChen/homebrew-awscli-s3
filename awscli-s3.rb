class AwscliS3 < Formula
  desc "Run remote scripts, Git and shell commands through S3"
  homepage "https://github.com/CabelChen/homebrew-awscli-s3"
  url "https://github.com/CabelChen/homebrew-awscli-s3/releases/download/v0.4.0/awscli-s3-0.4.0.tar.gz"
  version "0.4.0"
  sha256 "b239caf859903e901702c6399dbd70fb43186215ac9822f1c2c31202238584ee"

  depends_on "awscli"
  depends_on "python@3.14"
  uses_from_macos "git"

  def install
    libexec.install "s3_agent.py",
                    "s3_features.py",
                    "s3_repl.py",
                    "s3_codex.py",
                    "codex_worker.py",
                    "codex_mcp.py",
                    "install.py",
                    "install.sh",
                    "pack.py",
                    "config.example.ini",
                    "MANUAL.md",
                    "README.md",
                    "CODEX.md",
                    "s3-agent.service.example"
    (libexec/"examples").install "examples/hello.sh"
    (bin/"awscli").write <<~EOS
      #!/bin/sh
      exec "#{formula_opt_bin("python@3.14")}/python3.14" "#{libexec}/s3_agent.py" "$@"
    EOS
    chmod 0755, bin/"awscli"
    (bin/"awscli-mcp").write <<~EOS
      #!/bin/sh
      exec "#{formula_opt_bin("python@3.14")}/python3.14" "#{libexec}/codex_mcp.py" "$@"
    EOS
    chmod 0755, bin/"awscli-mcp"
  end

  def caveats
    <<~EOS
      The command is awscli. The official AWS command remains aws.
      Configuration: ~/.config/s3-agent/config.ini
      Example: #{libexec}/config.example.ini
      Manual: awscli help
      If upgrading from the user installer, use `which -a awscli` to check
      whether ~/.local/bin/awscli shadows the Homebrew command.
    EOS
  end

  test do
    assert_equal "awscli #{version}\n", shell_output("#{bin}/awscli --version")
    assert_match "中文使用手册", shell_output("#{bin}/awscli help")
    assert_match "已进入 bash", pipe_output("#{bin}/awscli bash", "exit\n", 0)
    script = testpath/"task.sh"
    script.write "printf 'homebrew-agent-ok\n'\n"
    store = "file://#{testpath}/store"
    task = shell_output("#{bin}/awscli --store #{store} submit -s demo #{script}").strip
    system bin/"awscli", "--store", store, "agent", "--name", "demo",
           "--work-dir", testpath/"work", "--once"
    result = shell_output("#{bin}/awscli --store #{store} result -s demo #{task}")
    assert_match '"status": "succeeded"', result
    assert_match "homebrew-agent-ok", result
  end
end
