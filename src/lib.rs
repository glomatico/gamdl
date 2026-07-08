use pyo3::prelude::*;

#[pyfunction]
fn native_available() -> bool {
    true
}

#[pymodule]
fn _amdecrypt(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(native_available, m)?)?;
    Ok(())
}
