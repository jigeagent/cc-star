#!/usr/bin/env node
/**
 * cc-star-mcp — MCP server launcher for cc-star hierarchical memory.
 *
 * This is a thin JavaScript wrapper that:
 * 1. Detects the system Python (3.10+)
 * 2. Auto-installs cc-star if not present
 * 3. Launches `python -m cc_star.mcp` as an MCP stdio server
 *
 * Usage:
 *   npx cc-star-mcp                    # Run MCP server (stdio)
 *   npx cc-star-mcp --help             # Show this help
 *   npx cc-star-mcp --version          # Show version
 *   npx cc-star-mcp --install          # Install/update cc-star Python package
 *
 * Claude Code MCP config:
 *   {
 *     "mcpServers": {
 *       "cc-star": {
 *         "command": "npx",
 *         "args": ["-y", "cc-star-mcp"]
 *       }
 *     }
 *   }
 */

const { spawn, execSync } = require("child_process");
const path = require("path");
const fs = require("fs");

const VERSION = "0.8.0";
const MIN_PYTHON_VER = [3, 10];
const PYPI_PACKAGE = "cc-star";

// ── Helpers ──

function findPython() {
  const candidates = process.platform === "win32"
    ? ["python", "python3", "py"]
    : ["python3", "python"];

  for (const cmd of candidates) {
    try {
      const out = execSync(`${cmd} --version`, { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
      const match = out.match(/Python (\d+)\.(\d+)/);
      if (match) {
        const major = parseInt(match[1]);
        const minor = parseInt(match[2]);
        if (major > MIN_PYTHON_VER[0] || (major === MIN_PYTHON_VER[0] && minor >= MIN_PYTHON_VER[1])) {
          return cmd;
        }
      }
    } catch {
      continue;
    }
  }
  return null;
}

function checkPackage(pythonCmd) {
  try {
    execSync(`${pythonCmd} -c "import cc_star; print(cc_star.__version__)"`, {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    return true;
  } catch {
    return false;
  }
}

function installPackage(pythonCmd) {
  console.error(`[cc-star] Installing ${PYPI_PACKAGE}...`);
  try {
    execSync(`${pythonCmd} -m pip install -U ${PYPI_PACKAGE}`, {
      encoding: "utf8",
      stdio: ["inherit"],
      timeout: 120000,
    });
    return true;
  } catch (e) {
    console.error(`[cc-star] Install failed: ${e.message}`);
    return false;
  }
}

// ── Main ──

function main() {
  const args = process.argv.slice(2);

  if (args.includes("--help") || args.includes("-h")) {
    console.log(`
cc-star-mcp v${VERSION}
Hierarchical memory MCP server for Claude Code

USAGE:
  npx cc-star-mcp                  Start MCP server (stdio)
  npx cc-star-mcp --install        Install/update Python package
  npx cc-star-mcp --version        Show version
  npx cc-star-mcp --help           Show this help

CLAUDE CODE CONFIG:
  Add to your claude_code settings.json:
    {
      "mcpServers": {
        "cc-star": {
          "command": "npx",
          "args": ["-y", "cc-star-mcp"]
        }
      }
    }
`);
    process.exit(0);
  }

  if (args.includes("--version") || args.includes("-v")) {
    console.log(VERSION);
    process.exit(0);
  }

  // Find Python
  const pythonCmd = findPython();
  if (!pythonCmd) {
    console.error("[cc-star] Python 3.10+ not found. Install Python first.");
    process.exit(1);
  }

  // Install if needed
  if (args.includes("--install")) {
    if (installPackage(pythonCmd)) {
      const ok = checkPackage(pythonCmd);
      if (ok) {
        const ver = execSync(`${pythonCmd} -c "import cc_star; print(cc_star.__version__)"`, { encoding: "utf8" }).trim();
        console.error(`[cc-star] ✅ ${PYPI_PACKAGE} v${ver} ready`);
      }
    }
    process.exit(0);
  }

  // Auto-install if not present
  if (!checkPackage(pythonCmd)) {
    console.error(`[cc-star] ${PYPI_PACKAGE} not found, installing...`);
    if (!installPackage(pythonCmd)) {
      console.error(`[cc-star] Failed to install ${PYPI_PACKAGE}. Run with --install to retry.`);
      process.exit(1);
    }
  }

  // Check if hmem is initialized
  try {
    const statusJson = execSync(
      `${pythonCmd} -c "import sys; sys.path.insert(0,'.'); from cc_star.hmem.store import HierarchicalStore; from cc_star.config import ConfigManager; from cc_star.cache.connection import CacheConnection; cfg=ConfigManager(); p=str(cfg.data_dir / 'hmem.db'); open(p); print('hmem ready')"`,
      { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 10000 }
    );
  } catch {
    console.error(
      "[cc-star] ⚠️  hmem.db not found. Run this after initialization:\n" +
      `  ${pythonCmd} -m cc_star hmem build`
    );
  }

  // Launch MCP server
  const child = spawn(pythonCmd, ["-m", "cc_star.mcp"], {
    stdio: ["pipe", "pipe", "inherit"],
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
  });

  // Pipe stdin/stdout for MCP stdio protocol
  process.stdin.pipe(child.stdin);
  child.stdout.pipe(process.stdout);

  child.on("exit", (code) => {
    process.exit(code ?? 0);
  });

  process.on("SIGINT", () => {
    child.kill("SIGINT");
  });

  process.on("SIGTERM", () => {
    child.kill("SIGTERM");
  });
}

main();
