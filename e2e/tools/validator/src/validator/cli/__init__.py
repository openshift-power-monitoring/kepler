# SPDX-FileCopyrightText: 2024-present Sunil Thaha <sthaha@redhat.com>
#
# SPDX-License-Identifier: APACHE-2.0

import datetime
import json
import logging
import os
import re
import subprocess
import time
import typing
from dataclasses import dataclass

import click
import matplotlib.pyplot as plt
import numpy as np
import numpy.typing as npt
from matplotlib import ticker
from matplotlib.dates import DateFormatter

from validator import config
from validator.__about__ import __version__
from validator.cli import options
from validator.prometheus import Comparator, PrometheusClient, Series, ValueOrError
from validator.report import CustomEncoder, JsonTemplate
from validator.specs import MachineSpec, get_host_spec, get_vm_spec
from validator.stresser import Remote, ScriptResult
from validator.validations import Loader, QueryTemplate, Validation

logger = logging.getLogger(__name__)
pass_config = click.make_pass_decorator(config.Validator)
data_dict = {}


@dataclass
class ValidationResult:
    name: str
    actual: str
    expected: str

    mse: ValueOrError
    mape: ValueOrError

    actual_dropped: int = 0
    expected_dropped: int = 0

    actual_filepath: str = ""
    expected_filepath: str = ""

    mse_passed: bool = True
    mape_passed: bool = True

    unexpected_error: str = ""

    def __init__(self, name: str, actual: str, expected: str) -> None:
        self.name = name
        self.actual = actual
        self.expected = expected

    @property
    def verdict(self) -> str:
        note = " (dropped)" if self.actual_dropped > 0 or self.expected_dropped > 0 else ""

        if self.unexpected_error or self.mse.error or self.mape.error:
            return f"ERROR{note}"

        if self.mse_passed and self.mape_passed:
            return f"PASS{note}"

        return f"FAIL{note}"


@dataclass
class ValidationResults:
    started_at: datetime.datetime
    ended_at: datetime.datetime
    results: list[ValidationResult]

    @property
    def passed(self) -> bool:
        return all(r.verdict == "PASS" for r in self.results)


@dataclass
class TestResult:
    tag: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    build_info: list[str]
    node_info: list[str]

    host_spec: MachineSpec

    validations: ValidationResults

    vm_spec: MachineSpec | None = None

    def __init__(self, tag: str) -> None:
        self.tag = tag

    @property
    def duration(self) -> datetime.timedelta:
        return self.end_time - self.start_time


class MarkdownReport:
    file: typing.TextIO

    def __init__(self, file: typing.TextIO) -> None:
        self.file = file

    def write(self, text: str) -> None:
        self.file.write(text)

    def close(self) -> None:
        self.flush()
        self.file.close()

    def flush(self) -> None:
        self.file.flush()

    def h1(self, text: str) -> None:
        self.write(f"# {text}\n\n")

    def h2(self, text: str) -> None:
        self.write(f"## {text}\n\n")

    def h3(self, text: str) -> None:
        self.write(f"### {text}\n\n")

    def h4(self, text: str) -> None:
        self.write(f"#### {text}\n\n")

    def code(self, text: str) -> None:
        self.write(f"```\n{text}\n```\n")

    def li(self, text: str) -> None:
        self.write(f"   - {text}\n")

    def img(self, alt_text: str, image_path: str) -> None:
        self.write(f"![{alt_text}]({image_path})\n")

    def table(self, headers: list[str], rows: list[list[str]]) -> None:
        self.write("| " + " | ".join(headers) + " |\n")
        self.write("| " + " | ".join(["---" for _ in headers]) + " |\n")
        for row in rows:
            self.write("| " + " | ".join(row) + " |\n")

    def add_machine_spec(self, title: str, ms: MachineSpec):
        self.h3(title)
        self.table(
            ["Model", "Sockets", "Cores", "Threads", "Flags"],
            [
                [
                    ms.cpu_spec.model,
                    ms.cpu_spec.sockets,
                    ms.cpu_spec.cores,
                    ms.cpu_spec.threads,
                    f"`{ms.cpu_spec.flags}`",
                ]
            ],
        )


def write_md_report(results_dir: str, r: TestResult):
    path = os.path.join(results_dir, f"report-{r.tag}.md")
    # ruff: noqa: SIM115 : suppressed use context handler
    md = MarkdownReport(open(path, "w"))

    md.h1(r.tag)
    md.h2("Build Info")
    for x in r.build_info:
        md.li(f"`{x}`")

    md.h2("Node Info")
    for x in r.node_info:
        md.li(f"`{x}`")

    md.h2("Machine Specs")
    md.add_machine_spec("Host", r.host_spec)
    if r.vm_spec is not None:
        md.add_machine_spec("VM", r.vm_spec)

    md.h2("Validation Results")
    md.li(f"Started At: `{r.start_time}`")
    md.li(f"Ended   At: `{r.end_time}`")
    md.li(f"Duration  : `{r.duration}`")

    md.h2("Validations")
    md.h3("Summary")
    md.table(
        ["Name", "MSE", "MAPE", "Pass / Fail"],
        [
            [v.name, f"{v.mse.value:.2f}", f"{v.mape.value:.2f}", v.verdict]
            for v in r.validations.results
            if not v.unexpected_error
        ],
    )

    md.h3("Details")
    for v in r.validations.results:
        md.h4(v.name)
        md.write("\n**Queries**:\n")
        md.li(f"Actual  : `{v.actual}`")
        md.li(f"Expected: `{v.expected}`")

        if v.unexpected_error:
            md.write("\n**Errors**:\n")
            md.code(v.unexpected_error)
            continue

        if v.actual_dropped or v.expected_dropped:
            md.write("\n**Dropped**:\n")
            md.li(f"Actual : `{v.actual_dropped}`")
            md.li(f"Expected: `{v.expected_dropped}`")

        md.write("\n**Results**:\n")
        md.li(f"MSE  : `{v.mse}`")
        md.li(f"MAPE : `{v.mape} %`")
        md.write("\n**Charts**:\n")
        img_path = create_charts_for_result(results_dir, v)
        md.img(v.name, img_path)

    md.close()

    click.secho("Report Generated ", fg="bright_green")
    click.secho(f" * report: {path} ", fg="bright_green")
    click.secho(f" * time-range:   {r.start_time} - {r.end_time}", fg="bright_green")


def extract_dates_and_values(json_path: str) -> tuple[npt.NDArray, list[float]]:
    with open(json_path) as f:
        json_data = json.load(f)

    timestamps = json_data["timestamps"]
    time_list = np.array([datetime.datetime.fromtimestamp(ts, tz=datetime.UTC) for ts in timestamps])
    values = json_data["values"]
    return time_list, values


def snake_case(s: str) -> str:
    return re.sub("[_-]+", "_", re.sub(r"[/\s]+", "_", s)).lower().strip()


def create_charts_for_result(results_dir: str, r: ValidationResult) -> str:
    actual_json_path = r.actual_filepath
    expected_json_path = r.expected_filepath

    images_dir = os.path.join(results_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    fig, ax = plt.subplots(figsize=(18, 7), sharex=True, sharey=True)
    plt.title(r.name)

    # actual in blue
    time, values = extract_dates_and_values(actual_json_path)
    ax.plot(time, values, marker="x", color="#024abf", label=r.actual)

    # expected in orange
    time, values = extract_dates_and_values(expected_json_path)
    ax.plot(time, values, marker="o", color="#ff742e", label=r.expected)

    # Set the x-axis tick format to display time
    ax.xaxis.set_major_formatter(DateFormatter("%H:%M:%S"))

    # Set the x-axis tick interval to 5 seconds
    ax.xaxis.set_major_locator(ticker.MaxNLocator(int((time[-1] - time[0]).total_seconds() / 10) + 1))
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")

    # TODO: add units to validation queries

    ax.legend(bbox_to_anchor=(0.5, -0.15), ncol=1)

    err_report = ""
    if r.mse.error is None:
        err_report += f"\nMSE: {r.mse.value:.2f}"

    if r.mape.error is None:
        err_report += f"\nMAPE: {r.mape.value:.2f}%"

    ax.text(
        0.98,
        1.10,
        err_report.lstrip(),
        transform=ax.transAxes,
        fontsize=14,
        verticalalignment="top",
        horizontalalignment="right",
        bbox={"facecolor": "white", "alpha": 0.5},
    )

    ax.grid(True)
    plt.tight_layout()

    # export it
    filename = snake_case(r.name)
    out_file = os.path.join(images_dir, f"{filename}.png")

    plt.savefig(out_file, format="png")

    return os.path.relpath(out_file, results_dir)


def create_report_dir(report_dir: str) -> tuple[str, str]:
    # run git describe command and get the output as the report name
    git_describe = subprocess.run(["/usr/bin/git", "describe", "--tag"], stdout=subprocess.PIPE, check=False)
    tag = git_describe.stdout.decode().strip()

    results_dir = os.path.join(report_dir, f"validator-{tag}")
    os.makedirs(results_dir, exist_ok=True)
    return results_dir, tag


def dump_query_result(raw_results_dir: str, query: QueryTemplate, series: Series) -> str:
    artifacts_dir = os.path.join(raw_results_dir, "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)

    filename = f"{query.metric_name}--{query.mode}.json"
    out_file = os.path.join(artifacts_dir, filename)

    with open(out_file, "w") as f:
        f.write(
            json.dumps(
                {
                    "query": query.one_line,
                    "metric": series.labels,
                    "timestamps": series.timestamps,
                    "values": series.values,
                }
            )
        )
    return out_file


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=False,
)
@click.version_option(version=__version__, prog_name="validator")
@click.option(
    "--log-level",
    "-l",
    type=click.Choice(["debug", "info", "warn", "error", "config"]),
    default="config",
    required=False,
)
@click.option(
    "--config-file",
    "-f",
    default="validator.yaml",
    type=click.Path(exists=True),
    show_default=True,
)
@click.pass_context
def validator(ctx: click.Context, config_file: str, log_level: str):
    cfg = config.load(config_file)
    log_level = cfg.log_level if log_level == "config" else log_level
    try:
        level = getattr(logging, log_level.upper())
    except AttributeError:
        # ruff: noqa: T201 (Suppressed as an early print statement before logging level is set)
        print(f"Invalid log level: {cfg.log_level}; setting to debug")
        level = logging.DEBUG

    logging.basicConfig(level=level)
    ctx.obj = cfg


@validator.command()
@click.option(
    "--script-path",
    "-s",
    default="./scripts/stressor.sh",
    type=click.Path(exists=True),
    show_default=True,
)
# ruff: noqa: S108 (Suppressed as we are intentionally using `/tmp` as reporting directory)
@click.option(
    "--report-dir",
    "-o",
    default="/tmp",
    type=click.Path(exists=True, dir_okay=True, writable=True),
    show_default=True,
)
@pass_config
def stress(cfg: config.Validator, script_path: str, report_dir: str):
    # run git describe command and get the output as the report name
    click.secho("  * Generating report dir and tag", fg="green")
    results_dir, tag = create_report_dir(report_dir)
    click.secho(f"\tresults dir: {results_dir}, tag: {tag}", fg="bright_green")
    filepath = results_dir + "/" + tag + ".json"
    data_dict.update({"file_path": filepath})

    res = TestResult(tag)

    res.build_info, res.node_info = get_build_and_node_info(cfg.prometheus)

    click.secho("  * Generating spec report ...", fg="green")
    res.host_spec = get_host_spec()
    res.vm_spec = get_vm_spec(cfg.remote)

    click.secho("  * Running stress test ...", fg="green")
    remote = Remote(cfg.remote)
    stress_test = remote.run_script(script_path)
    res.start_time = stress_test.start_time
    res.end_time = stress_test.end_time

    # sleep a bit for prometheus to finish scrapping
    click.secho("  * Sleeping for 10 seconds ...", fg="green")
    time.sleep(10)

    res.validations = run_validations(cfg, stress_test, results_dir)
    create_json(res)
    write_md_report(results_dir, res)


@validator.command()
@click.option("--start", "-s", type=options.DateTime(), required=True)
@click.option("--end", "-e", type=options.DateTime(), required=True)
# ruff: noqa: S108 (Suppressed as we are intentionally using `/tmp` as reporting directory)
@click.option(
    "--report-dir",
    "-o",
    default="/tmp",
    type=click.Path(exists=True),
)
@pass_config
def gen_report(cfg: config.Validator, start: datetime.datetime, end: datetime.datetime, report_dir: str):
    """
    Run only validation based on previously stress test
    """
    click.secho("  * Generating report dir and tag", fg="green")
    results_dir, tag = create_report_dir(report_dir)
    click.secho(f"\tresults dir: {results_dir}, tag: {tag}", fg="bright_green")

    res = TestResult(tag)
    res.start_time = start
    res.end_time = end

    res.build_info, res.node_info = get_build_and_node_info(cfg.prometheus)

    click.secho("  * Generating spec report ...", fg="green")
    res.host_spec = get_host_spec()
    res.vm_spec = get_vm_spec(cfg.remote)

    script_result = ScriptResult(start, end)
    res.validations = run_validations(cfg, script_result, results_dir)

    write_md_report(results_dir, res)


def get_build_and_node_info(prom_config: config.Prometheus) -> tuple[list[str], list[str]]:
    prom = PrometheusClient(prom_config)
    build_info = prom.kepler_build_info()

    click.secho("\n  * Build Info", fg="green")
    for bi in build_info:
        click.secho(f"    - {bi}", fg="cyan")

    node_info = prom.kepler_node_info()
    click.secho("\n  * Node Info", fg="green")
    for ni in node_info:
        click.secho(f"    - {ni}", fg="cyan")

    click.echo()
    return build_info, node_info


def run_validations(cfg: config.Validator, test: ScriptResult, results_dir: str) -> ValidationResults:
    start_time, end_time = test.start_time, test.end_time

    click.secho("  * Generating validation report", fg="green")
    click.secho(f"   - started at: {start_time}", fg="green")
    click.secho(f"   - ended   at: {end_time}", fg="green")
    click.secho(f"   - duration  : {end_time - start_time}", fg="green")

    click.secho("\nRun validations", fg="green")
    prom = PrometheusClient(cfg.prometheus)
    comparator = Comparator(prom)

    validations = Loader(cfg).load()
    results = [run_validation(v, comparator, start_time, end_time, results_dir) for v in validations]

    return ValidationResults(started_at=start_time, ended_at=end_time, results=results)


def run_validation(
    v: Validation,
    comparator: Comparator,
    start_time: datetime.datetime,
    end_time: datetime.datetime,
    results_dir: str,
) -> ValidationResult:
    result = ValidationResult(
        v.name,
        v.actual.one_line,
        v.expected.one_line,
    )

    click.secho(f"{v.name}", fg="cyan")
    click.secho(f"  - actual  :  {v.actual.one_line}")
    click.secho(f"  - expected:  {v.expected.one_line}")

    try:
        cmp = comparator.compare(
            start_time,
            end_time,
            v.actual.promql,
            v.expected.promql,
        )
        click.secho(f"\t MSE : {cmp.mse}", fg="bright_blue")
        click.secho(f"\t MAPE: {cmp.mape} %\n", fg="bright_blue")

        result.expected_dropped = cmp.expected_dropped
        result.actual_dropped = cmp.expected_dropped

        if cmp.expected_dropped > 0 or cmp.actual_dropped > 0:
            logger.warning(
                "dropped %d samples from expected and %d samples from actual",
                cmp.expected_dropped,
                cmp.actual_dropped,
            )

        result.mse, result.mape = cmp.mse, cmp.mape

        result.mse_passed = v.max_mse is None or (cmp.mse.error is None and cmp.mse.value <= v.max_mse)
        result.mape_passed = v.max_mape is None or (cmp.mape.error is None and cmp.mape.value <= v.max_mape)

        if not result.mse_passed:
            click.secho(f"MSE exceeded threshold. mse: {cmp.mse}, max_mse: {v.max_mse}", fg="red")

        if not result.mape_passed:
            click.secho(f"MAPE exceeded threshold. mape: {cmp.mape}, max_mape: {v.max_mape}", fg="red")

        result.actual_filepath = dump_query_result(results_dir, v.expected, cmp.actual_series)
        result.expected_filepath = dump_query_result(results_dir, v.actual, cmp.expected_series)

    # ruff: noqa: BLE001 (Suppressed as we want to catch all exceptions here)
    except Exception as e:
        click.secho(f"\t    {v.name} failed: {e} ", fg="red")
        click.secho(f"\t    Error: {e} ", fg="yellow")
        result.unexpected_error = str(e)

    return result


@validator.command()
@click.option("--duration", "-d", type=options.Duration(), required=True)
# ruff: noqa: S108 (Suppressed as we are intentionally using `/tmp` as reporting directory)
@click.option(
    "--report-dir",
    "-o",
    default="/tmp",
    type=click.Path(exists=True, dir_okay=True, writable=True),
    show_default=True,
)
@pass_config
def validate_acpi(cfg: config.Validator, duration: datetime.timedelta, report_dir: str) -> int:
    results_dir, tag = create_report_dir(report_dir)
    res = TestResult(tag)

    res.end_time = datetime.datetime.now(tz=datetime.UTC)
    res.start_time = res.end_time - duration

    click.secho("  * Generating build and node info ...", fg="green")
    res.build_info, res.node_info = get_build_and_node_info(cfg.prometheus)

    click.secho("  * Generating spec report ...", fg="green")
    res.host_spec = get_host_spec()

    script_result = ScriptResult(res.start_time, res.end_time)
    res.validations = run_validations(cfg, script_result, results_dir)

    click.secho("  * Generating validate acpi report file and dir", fg="green")
    write_md_report(results_dir, res)

    return int(res.validations.passed)


def create_json(res):
    def update_list_json(new_value: list, new_key: str):
        data_dict[new_key] = new_value

    def custom_encode(input_string):
        pattern = re.compile(r'(\w+)=("[^"]*"|[^,]+)')
        matches = pattern.findall(input_string)
        parsed_dict = {key: value.strip('"') for key, value in matches}
        return json.dumps(parsed_dict)

    result = []

    for i in res.validations.results:
        value = {}
        if i.mse_passed:
            value["mse"] = float(i.mse.value)
        else:
            value["mse"] = float(i.mse.error)
        if i.mape_passed:
            value["mape"] = float(i.mape.value)
        else:
            value["mape"] = float(i.mape.error)
        value["status"] = "mape passed: " + str(i.mape_passed) + ", mse passed: " + str(i.mse_passed)
        m_name = i.name.replace(" - ", "_")

        result.append({m_name: value})

    build_info = []
    for i in res.build_info:
        tmp = i.replace("kepler_exporter_build_info", "")
        build_info.append(custom_encode(tmp))

    node_info = []
    for i in res.node_info:
        tmp = i.replace("kepler_exporter_node_info", "")
        node_info.append(custom_encode(tmp))

    update_list_json(build_info, "build_info")
    update_list_json(node_info, "node_info")

    machine_spec = []
    machine_spec.append(
        {
            "type": "host",
            "model": res.host_spec[0][0],
            "cores": res.host_spec[0][1],
            "threads": res.host_spec[0][2],
            "sockets": res.host_spec[0][3],
            "flags": res.host_spec[0][4],
            "dram": res.host_spec[1],
        }
    )
    machine_spec.append(
        {
            "type": "vm",
            "model": res.vm_spec[0][0],
            "cores": res.vm_spec[0][1],
            "threads": res.vm_spec[0][2],
            "sockets": res.vm_spec[0][3],
            "flags": res.vm_spec[0][4],
            "dram": res.vm_spec[1],
        }
    )
    update_list_json(machine_spec, "machine_spec")

    update_list_json(result, "result")
    json_template = JsonTemplate(**data_dict)
    file_name = data_dict["file_path"]
    with open(file_name, "w") as file:
        json.dump(json_template, file, cls=CustomEncoder, indent=2)
