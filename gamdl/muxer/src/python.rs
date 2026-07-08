use crate::decrypt::WrapperDecryptSession;
use pyo3::prelude::*;

#[pyfunction]
fn native_available() -> bool {
    true
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(native_available, module)?)?;
    module.add_class::<WrapperDecryptSession>()?;
    Ok(())
}
